"""Configurations."""
import os
from dataclasses import dataclass, field
from typing import Any, Dict

import httpx
from jwt import PyJWKClient
from tenacity import retry, stop_after_attempt, stop_after_delay, wait_fixed
from yaml import safe_load

import models
from db.adapter import ResourcePoolRepository, UserRepository
from models import errors
from users.credentials import KeycloakAuthenticator
from users.dummy import DummyAuthenticator, DummyUserStore
from users.keycloak import KcUserStore


@retry(stop=(stop_after_attempt(20) | stop_after_delay(300)), wait=wait_fixed(2), reraise=True)
def _oidc_discovery(url: str, realm: str) -> Dict[str, Any]:
    url = f"{url}/realms/{realm}/.well-known/openid-configuration"
    res = httpx.get(url)
    if res == 200:
        return res.json()
    raise errors.ConfigurationError(message=f"Cannot successfully do OIDC discovery with url {url}.")


@dataclass
class Config:
    """Configuration for the CRAC service."""

    user_repo: UserRepository
    rp_repo: ResourcePoolRepository
    user_store: models.UserStore
    authenticator: models.Authenticator
    spec_file: str = "src/api.spec.yaml"
    spec: Dict[str, Any] = field(init=False, default_factory=dict)
    version: str = "0.0.1"
    app_name: str = "renku_crac"

    def __post_init__(self):
        with open(self.spec_file, "r") as f:
            self.spec = safe_load(f)

    @classmethod
    def from_env(cls):
        """Create a config from environment variables."""

        prefix = ""
        user_store: models.UserStore
        authenticator: models.Authenticator
        version = os.environ.get(f"{prefix}VERSION", "0.0.1")
        keycloak_url = None
        keycloak_realm = "Renku"

        if os.environ.get(f"{prefix}DUMMY_STORES", "false").lower() == "true":
            async_sqlalchemy_url = os.environ.get(
                f"{prefix}ASYNC_SQLALCHEMY_URL", "sqlite+aiosqlite:///data_services.db"
            )
            sync_sqlalchemy_url = os.environ.get(f"{prefix}SYNC_SQLALCHEMY_URL", "sqlite:///data_services.db")
            authenticator = DummyAuthenticator(admin=True)
            user_always_exists = os.environ.get("DUMMY_USERSTORE_USER_ALWAYS_EXISTS", "true").lower() == "true"
            user_store = DummyUserStore(user_always_exists=user_always_exists)
        else:
            async_sqlalchemy_url = os.environ.get(f"{prefix}ASYNC_SQLALCHEMY_URL", "")
            sync_sqlalchemy_url = os.environ.get(f"{prefix}SYNC_SQLALCHEMY_URL", "")
            if async_sqlalchemy_url == "" or sync_sqlalchemy_url == "":
                raise errors.ConfigurationError(message="The sqlalchemy url has to be specified.")
            keycloak_url = os.environ.get(f"{prefix}KEYCLOAK_URL")
            if keycloak_url is None:
                raise errors.ConfigurationError(message="The Keycloak URL has to be specified.")
            keycloak_realm = os.environ.get(f"{prefix}KEYCLOAK_REALM", "Renku")
            oidc_disc_data = _oidc_discovery(keycloak_url, keycloak_realm)
            jwks_url = oidc_disc_data.get("jwks_uri")
            if jwks_url is None:
                raise errors.ConfigurationError(
                    message="The JWKS url for Keycloak cannot be found from the OIDC discovery endpoint."
                )
            algorithms = os.environ.get(f"{prefix}KEYCLOAK_TOKEN_SIGNATURE_ALGS")
            if algorithms is None:
                raise errors.ConfigurationError(message="At least one token signature algorithm is requried.")
            algorithms_lst = [i.strip() for i in algorithms.split(",")]
            jwks = PyJWKClient(jwks_url)
            authenticator = KeycloakAuthenticator(jwks=jwks, algorithms=algorithms_lst)
            user_store = KcUserStore(keycloak_url=keycloak_url, realm=keycloak_realm)

        user_repo = UserRepository(sync_sqlalchemy_url=sync_sqlalchemy_url, async_sqlalchemy_url=async_sqlalchemy_url)
        rp_repo = ResourcePoolRepository(
            sync_sqlalchemy_url=sync_sqlalchemy_url, async_sqlalchemy_url=async_sqlalchemy_url
        )
        return cls(
            user_repo=user_repo, rp_repo=rp_repo, version=version, authenticator=authenticator, user_store=user_store
        )
