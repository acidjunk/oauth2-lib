# Copyright 2019-2020 SURF.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import re
from http import HTTPStatus
from json import JSONDecodeError
from typing import Any, AsyncGenerator, Callable, Coroutine, List, Mapping, Optional, Set, cast

from fastapi.exceptions import HTTPException
from fastapi.param_functions import Depends
from fastapi.requests import Request
from fastapi.security.http import HTTPBearer
from httpx import AsyncClient, BasicAuth, NetworkError
from pydantic import BaseModel
from starlette.requests import ClientDisconnect
from structlog import get_logger

logger = get_logger(__name__)


class OIDCUserModel(dict):
    """The standard claims of a OIDCUserModel object. Defined per `Section 5.1`_.

    .. _`Section 5.1`: http://openid.net/specs/openid-connect-core-1_0.html#StandardClaims
    """

    #: registered claims that OIDCUserModel supports
    REGISTERED_CLAIMS = [
        "sub",
        "name",
        "given_name",
        "family_name",
        "middle_name",
        "nickname",
        "preferred_username",
        "profile",
        "picture",
        "website",
        "email",
        "email_verified",
        "gender",
        "birthdate",
        "zoneinfo",
        "locale",
        "phone_number",
        "phone_number_verified",
        "address",
        "updated_at",
    ]

    def __getattr__(self, key: str) -> Any:
        try:
            return object.__getattribute__(self, key)
        except AttributeError as error:
            if key in self.REGISTERED_CLAIMS:
                return self.get(key)
            raise error

    """
    Below this line are SURFnet specific properties that are used often in code to either display or check on.
    """

    @property
    def user_name(self) -> str:
        if "user_name" in self.keys():
            return cast(str, self["user_name"])
        elif "unspecified_id" in self.keys():
            return cast(str, self["unspecified_id"])
        else:
            return ""

    @property
    def display_name(self) -> str:
        return self.get("display_name", "")

    @property
    def principal_name(self) -> str:
        return self.get("eduperson_principal_name", "")

    @property
    def memberships(self) -> List[str]:
        return self.get("edumember_is_member_of", [])

    @property
    def teams(self) -> Set[str]:
        prefix = "urn:collab:group:surfteams.nl:nl:surfnet:diensten:"
        length = len(prefix)
        return {urn[length:] for urn in self.memberships if urn.startswith(prefix)}

    @property
    def entitlements(self) -> List[str]:
        return self.get("eduperson_entitlement", [])

    @property
    def roles(self) -> Set[str]:
        prefix = "urn:mace:surfnet.nl:surfnet.nl:sab:role:"
        length = len(prefix)
        return {urn[length:] for urn in self.entitlements if urn.startswith(prefix)}

    @property
    def organization_codes(self) -> Set[str]:
        prefix = "urn:mace:surfnet.nl:surfnet.nl:sab:organizationCode:"
        length = len(prefix)
        return {urn[length:] for urn in self.entitlements if urn.startswith(prefix)}

    @property
    def organization_guids(self) -> Set[str]:
        prefix = "urn:mace:surfnet.nl:surfnet.nl:sab:organizationGUID:"
        length = len(prefix)
        return {urn[length:] for urn in self.entitlements if urn.startswith(prefix)}

    @property
    def scopes(self) -> Set[str]:
        if isinstance(self.get("scope"), list):
            return set(self.get("scope"))  # type: ignore
        return set(re.split("[ ,]", self.get("scope", "")))


async def async_client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient() as client:
        yield client


class OIDCConfig(BaseModel):
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    introspect_endpoint: str
    jwks_uri: str
    response_types_supported: List[str]
    response_modes_supported: List[str]
    grant_types_supported: List[str]
    subject_types_supported: List[str]
    id_token_signing_alg_values_supported: List[str]
    scopes_supported: List[str]
    token_endpoint_auth_methods_supported: List[str]
    claims_supported: List[str]
    claims_parameter_supported: bool
    request_parameter_supported: bool
    code_challenge_methods_supported: List[str]


class OPAResult(BaseModel):
    result: bool = False
    decision_id: str


class OIDCUser(HTTPBearer):
    """
    OIDCUser class extends the HTTPBearer class to do extra verification.

    The class will act as follows:
        1. Validate the Credentials at SURFconext by calling the UserInfo endpoint
        2. When receiving an active token it will enrich the response through the database roles
    """

    openid_config: Optional[OIDCConfig] = None
    openid_url: str
    resource_server_id: str
    resource_server_secret: str
    enabled: bool

    def __init__(
        self,
        openid_url: str,
        resource_server_id: str,
        resource_server_secret: str,
        enabled: bool = True,
        auto_error: bool = True,
        scheme_name: Optional[str] = None,
    ):
        super().__init__(auto_error=auto_error)
        self.openid_url = openid_url
        self.resource_server_id = resource_server_id
        self.resource_server_secret = resource_server_secret
        self.enabled = enabled
        self.scheme_name = scheme_name or self.__class__.__name__

    async def __call__(  # type: ignore
        self, request: Request, async_client: AsyncClient = Depends(async_client)
    ) -> Optional[OIDCUserModel]:
        """
        Return the OIDC user from OIDC introspect endpoint.

        This is used as a security module in Fastapi projects

        Args:
            request: Starlette request method.
            async_client: The httpx client

        Returns:
            OIDCUserModel object.

        """
        if self.enabled:

            await self.check_openid_config(async_client)

            credentials = await super().__call__(request)
            if credentials:
                user_info = await self.introspect_token(async_client, credentials.credentials)

                if not user_info.get("active", False):
                    logger.debug("Token is invalid")
                    raise HTTPException(status_code=HTTPStatus.UNAUTHORIZED, detail="Access token is invalid")

                logger.debug("OIDCUserModel object.", user_info=user_info)
                return user_info

        return None

    async def check_openid_config(self, async_client: AsyncClient) -> None:
        """Check of openid config is loaded and load if not."""
        if self.openid_config is not None:
            return

        response = await async_client.get(self.openid_url + "/.well-known/openid-configuration")
        self.openid_config = OIDCConfig.parse_obj(response.json())

    async def introspect_token(self, async_client: AsyncClient, token: str) -> OIDCUserModel:
        """
        Introspect the access token to retrieve the user info.

        Args:
            token: the access_token

        Returns:
            OIDCUserModel from openid server

        """
        await self.check_openid_config(async_client)
        assert self.openid_config

        response = await async_client.post(
            self.openid_config.introspect_endpoint,
            params={"token": token},
            auth=BasicAuth(self.resource_server_id, self.resource_server_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            data = dict(response.json())
        except JSONDecodeError:
            logger.debug("Unable to parse introspect response")
            raise HTTPException(status_code=HTTPStatus.UNAUTHORIZED, detail=response.text)
        logger.debug("Response from openid introspect", response=data)

        if response.status_code not in range(200, 300):
            logger.debug("Introspect cannot find an active token, user unauthorized")

            raise HTTPException(status_code=HTTPStatus.UNAUTHORIZED, detail=response.text)

        return OIDCUserModel(data)


def opa_decision(
    opa_url: str,
    oidc_security: OIDCUser,
    enabled: bool = True,
    auto_error: bool = True,
    opa_kwargs: Optional[Mapping[str, str]] = None,
) -> Callable[[Request, OIDCUserModel, AsyncClient], Coroutine[Any, Any, Optional[bool]]]:
    async def _opa_decision(
        request: Request,
        user_info: OIDCUserModel = Depends(oidc_security),
        async_request: AsyncClient = Depends(async_client),
    ) -> Optional[bool]:
        """
        Check OIDCUserModel against the OPA policy.

        This is used as a security module in Fastapi projects
        This method will make an async call towards the Policy agent.

        Args:
            request: Request object that will be used to retrieve request metadata.
            user_info: The OIDCUserModel object that will be checked
        """

        if enabled:
            try:
                json = await request.json()
            # Silencing the Decode error or Type error when request.json() does not return anything sane.
            # Some requests do not have a json respone therefore as this code gets called on every request
            # we need to suppress the `None` case (TypeError) or the `other than json` case (JSONDecodeError)
            except (JSONDecodeError, TypeError, ClientDisconnect):
                json = {}

            opa_input = {
                "input": {
                    **(opa_kwargs or {}),
                    **user_info,
                    "resource": request.url.path,
                    "method": request.method,
                    "arguments": {"path": request.path_params, "query": {**request.query_params}, "json": json},
                }
            }

            logger.debug("Posting input json to Policy agent", input=opa_input)

            try:
                result = await async_request.post(opa_url, json=opa_input)
            except (NetworkError, TypeError):
                raise HTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail="Policy agent is unavailable")

            data = OPAResult.parse_obj(result.json())

            if not data.result and auto_error:
                logger.warning(
                    "User is not allowed to access the resource",
                    decision_id=data.decision_id,
                    resource=request.url.path,
                    method=request.method,
                    user_info=user_info,
                    input=opa_input,
                )
                raise HTTPException(
                    status_code=HTTPStatus.FORBIDDEN,
                    detail=f"User is not allowed to access resource: {request.url.path} Decision was taken with id: {data.decision_id}",
                )
            else:
                if data.result:
                    logger.debug(
                        "User is authorized to access the resource",
                        decision_id=data.decision_id,
                        resource=request.url.path,
                        method=request.method,
                        user_info=user_info,
                        input=opa_input,
                    )

                return data.result

        return None

    return _opa_decision
