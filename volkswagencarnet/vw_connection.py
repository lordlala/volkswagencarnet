#!/usr/bin/env python3
"""Communicate with We Connect services."""
from __future__ import annotations

import hashlib
import re
import secrets
import sys
import time
from base64 import b64encode, urlsafe_b64encode
from datetime import timedelta, datetime
from random import random, randint
from sys import version_info

import asyncio
import jwt
import logging
from aiohttp import ClientSession, ClientTimeout, client_exceptions
from aiohttp.hdrs import METH_GET, METH_POST
from bs4 import BeautifulSoup
from json import dumps as to_json
from urllib.parse import urljoin, parse_qs, urlparse

from volkswagencarnet.vw_exceptions import AuthenticationException
from volkswagencarnet.vw_timer import TimerData, TimersAndProfiles
from .vw_const import (
    BRAND,
    COUNTRY,
    HEADERS_SESSION,
    HEADERS_SESSION_NA,
    HEADERS_AUTH,
    BASE_SESSION,
    BASE_AUTH,
    BASE_AUTH_NA,
    CLIENT,
    XCLIENT_ID,
    XAPPVERSION,
    XAPPNAME,
    USER_AGENT,
    APP_URI,
    APP_URI_NA
)
from .vw_utilities import json_loads, read_config
from .vw_vehicle import Vehicle

MAX_RETRIES_ON_RATE_LIMIT = 3

version_info >= (3, 7) or exit("Python 3.7+ required")

_LOGGER = logging.getLogger(__name__)

TIMEOUT = timedelta(seconds=30)
JWT_ALGORITHMS = ["RS256"]


# noinspection PyPep8Naming
class Connection:
    """Connection to VW-Group Connect services."""

    # Init connection class
    def __init__(self, session, username, password, fulldebug=False, country=COUNTRY, interval=timedelta(minutes=5)):
        """Initialize."""
        self._x_client_id = None
        self._session = session
        self._session_fulldebug = fulldebug
        self._session_headers = HEADERS_SESSION.copy()
        self._session_base = BASE_SESSION
        self._session_auth_headers = HEADERS_AUTH.copy()
        self._session_auth_base = BASE_AUTH
        self._session_refresh_interval = interval

        no_vin_key = ""
        self._session_auth_ref_urls = {no_vin_key: BASE_SESSION}
        self._session_spin_ref_urls = {no_vin_key: BASE_SESSION}
        self._session_logged_in = False
        self._session_first_update = False
        self._session_auth_username = username
        self._session_auth_password = password
        self._session_tokens = {}
        self._session_country = country.upper()

        self._vehicles = []

        _LOGGER.debug(f"Using service {self._session_base}")

        self._jarCookie = ""
        self._state = {}

    def _clear_cookies(self):
        self._session._cookie_jar._cookies.clear()

    # API Login
    async def doLogin(self, tries: int = 1):
        """Login method, clean login."""
        _LOGGER.debug("Initiating new login")

        for i in range(tries):
            self._session_logged_in = await self._login("Legacy")
            if self._session_logged_in:
                break
            _LOGGER.info("Something failed")
            await asyncio.sleep(random() * 5)

        if not self._session_logged_in:
            return False

        _LOGGER.info("Successfully logged in")
        self._session_tokens["identity"] = self._session_tokens["Legacy"].copy()
        self._session_logged_in = True

        # Get VW-Group API tokens
        if not await self._getAPITokens():
            self._session_logged_in = False
            return False

        # Get list of vehicles from account
        _LOGGER.debug("Fetching vehicles associated with account")
        await self.set_token("vwg")
        self._session_headers.pop("Content-Type", None)
        loaded_vehicles = await self.get(
            url=f"{BASE_SESSION}/fs-car/usermanagement/users/v1/{BRAND}/{self._session_country}/vehicles"
        )
        # Add Vehicle class object for all VIN-numbers from account
        if loaded_vehicles.get("userVehicles") is not None:
            _LOGGER.debug("Found vehicle(s) associated with account.")
            for vehicle in loaded_vehicles.get("userVehicles").get("vehicle"):
                self._vehicles.append(Vehicle(self, vehicle))
        else:
            _LOGGER.warning("Failed to login to We Connect API.")
            self._session_logged_in = False
            return False

        # Update all vehicles data before returning
        await self.set_token("vwg")
        await self.update()
        return True

    async def _login(self, client="Legacy"):
        """Login function."""

        # Helper functions
        def getNonce():
            """
            Get a random nonce.

            :return:
            """
            ts = "%d" % (time.time())
            sha256 = hashlib.sha256()
            sha256.update(ts.encode())
            sha256.update(secrets.token_bytes(16))
            return b64encode(sha256.digest()).decode("utf-8")[:-1]

        def base64URLEncode(s):
            """
            Encode string as Base 64 in a URL safe way, stripping trailing '='.

            :param s:
            :return:
            """
            return urlsafe_b64encode(s).rstrip(b"=")

        # Login starts here
        try:
            # Get OpenID config:
            self._clear_cookies()
            if self._session_country == 'DE':
                url = f"{BASE_AUTH}/.well-known/openid-configuration"
            else:
                #self._session_headers = HEADERS_SESSION_NA.copy()
                url = f"{BASE_AUTH_NA}/.well-known/openid-configuration"
            self._session_headers = HEADERS_SESSION.copy()
            self._session_auth_headers = HEADERS_AUTH.copy()
            if self._session_fulldebug:
                _LOGGER.debug("Requesting openid config")
            req = await self._session.get(url=url)
            if req.status != 200:
                _LOGGER.debug("OpenId config error")
                return False
            response_data = await req.json()
            authorization_endpoint = response_data["authorization_endpoint"]
            auth_issuer = response_data["issuer"]
            apigw_requestid = req.headers.get("apigw-requestid")

            # Get authorization page (login page)
            # https://identity.vwgroup.io/oidc/v1/authorize?nonce={NONCE}&state={STATE}&response_type={TOKEN_TYPES}&scope={SCOPE}&redirect_uri={APP_URI}&client_id={CLIENT_ID}
            if self._session_fulldebug:
                _LOGGER.debug(f'Get authorization page from "{authorization_endpoint}"')
                self._session_auth_headers.pop("Referer", None)
                self._session_auth_headers.pop("Origin", None)
                _LOGGER.debug(f'Request headers: "{self._session_auth_headers}"')
            try:
                code_verifier = base64URLEncode(secrets.token_bytes(32))
                if len(code_verifier) < 43:
                    raise ValueError("Verifier too short. n_bytes must be > 30.")
                elif len(code_verifier) > 128:
                    raise ValueError("Verifier too long. n_bytes must be < 97.")
                challenge = base64URLEncode(hashlib.sha256(code_verifier).digest())

                if self._session_country == "DE":
                    params={
                        "redirect_uri": APP_URI,
                        "prompt": "login",
                        "nonce": getNonce(),
                        "state": getNonce(),
                        "code_challenge_method": "s256",
                        "code_challenge": challenge.decode(),
                        "response_type": CLIENT[client].get("TOKEN_TYPES"),
                        "client_id": CLIENT[client].get("CLIENT_ID"),
                        "scope": CLIENT[client].get("SCOPE"),
                    }
                else:
                    #from requests.utils import quote
                    params={
                        "redirect_uri": "https://carnet.vw.com/login",
                        "prompt": "login",
                        #"nonce": getNonce(),
                        "state": getNonce(),
                        #"code_challenge_method": "s256",
                        "code_challenge": challenge.decode(),
                        "response_type": 'code',
                        "client_id": CLIENT[client].get("CLIENT_ID_NA"),
                        "scope": CLIENT[client].get("SCOPE"),
                        "ui_locales": "en-US",
                    }
                    self._session_auth_headers = HEADERS_SESSION_NA



                self._session_auth_headers['Accept'] = 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7'

                req = await self._session.get(
                    #url=authorization_endpoint,
                    url='https://b-h-s.spr.us00.p.con-veh.net/oidc/v1/authorize',
                    #url=authorization_endpoint,
                    url='https://b-h-s.spr.us00.p.con-veh.net/oidc/v1/authorize',
                    #headers=self._session_auth_headers,
                    headers=self._session_auth_headers,
                    allow_redirects=False,
                    params=params
                )

                if req.headers.get("Location", False):
                    ref = urljoin(authorization_endpoint, req.headers.get("Location", ""))
                    if "error" in ref:
                        error = parse_qs(urlparse(ref).query).get("error", "")[0]
                        if "error_description" in ref:
                            error_description = parse_qs(urlparse(ref).query).get("error_description", "")[0]
                            _LOGGER.info(f"Unable to login, {error_description}")
                        else:
                            _LOGGER.info("Unable to login.")
                        raise Exception(error)
                    else:
                        if self._session_fulldebug:
                            _LOGGER.debug(f'Got redirect to "{ref}"')
                        req = await self._session.get(
                            url=ref, headers=self._session_auth_headers, allow_redirects=False
                        )
                else:
                    _LOGGER.warning("Unable to fetch authorization endpoint.")
                    raise Exception('Missing "location" header')
            except Exception as error:
                _LOGGER.warning("Failed to get authorization endpoint")
                raise error

            if req.status != 302:
                raise Exception("Fetching authorization endpoint failed")
            else:
                _LOGGER.debug("Got authorization endpoint")

            try:
                #ref = req.headers.get("Location")
                ref = urljoin(authorization_endpoint, req.headers.get("Location", ""))
                params = {
                    'relayState': getNonce()
                }

                req = await self._session.get(
                    url=ref, headers=self._session_auth_headers, allow_redirects=False
                )
            except Exception as error:
                _LOGGER.warning("Failed to get authorization endpoint")
                raise error

            if req.status != 200:
                raise Exception("Fetching authorization endpoint failed")
            elif req.status == 302:
                pass
            else:
                _LOGGER.debug("Got authorization endpoint")
            try:
                response_data = await req.text()
                response_soup = BeautifulSoup(response_data, "html.parser")
                mailform = {
                    t["name"]: t["value"]
                    for t in response_soup.find("form", id="emailPasswordForm").find_all("input", type="hidden")
                }
                mailform["email"] = self._session_auth_username
                pe_url = auth_issuer + response_soup.find("form", id="emailPasswordForm").get("action")
            except Exception as e:
                _LOGGER.error("Failed to extract user login form.")
                raise e

            # POST email
            # https://identity.vwgroup.io/signin-service/v1/{CLIENT_ID}/login/identifier
            self._session_auth_headers["Referer"] = authorization_endpoint
            self._session_auth_headers["Origin"] = auth_issuer
            req = await self._session.post(url=pe_url, headers=self._session_auth_headers, data=mailform)
            if req.status != 200:
                raise Exception("POST password request failed")
            try:
                response_data = await req.text()
                response_soup = BeautifulSoup(response_data, "html.parser")
                pw_form: dict[str, str] = {}
                post_action = None
                client_id = None
                for d in response_soup.find_all("script"):
                    if "src" in d.attrs:
                        continue
                    if "window._IDK" in d.string:
                        if re.match('"errorCode":"', d.string) is not None:
                            raise Exception("Error code in response")
                        pw_form["relayState"] = re.search('"relayState":"([a-f0-9]*)"', d.string)[1]
                        pw_form["hmac"] = re.search('"hmac":"([a-f0-9]*)"', d.string)[1]
                        pw_form["email"] = re.search('"email":"([^"]*)"', d.string)[1]
                        pw_form["_csrf"] = re.search("csrf_token:\\s*'([^\"']*)'", d.string)[1]
                        post_action = re.search('"postAction":\\s*"([^"\']*)"', d.string)[1]
                        client_id = re.search('"clientId":\\s*"([^"\']*)"', d.string)[1]
                        break
                if pw_form["hmac"] is None or post_action is None:
                    raise Exception("Failed to find authentication data in response")
                pw_form["password"] = self._session_auth_password
                pw_url = "{host}/signin-service/v1/{clientId}/{postAction}".format(
                    host=auth_issuer, clientId=client_id, postAction=post_action
                )
            except Exception as e:
                _LOGGER.error("Failed to extract password login form.")
                raise e

            # POST password
            # https://identity.vwgroup.io/signin-service/v1/{CLIENT_ID}/login/authenticate
            self._session_auth_headers["Referer"] = pe_url
            self._session_auth_headers["Origin"] = auth_issuer
            _LOGGER.debug("Authenticating with email and password.")
            if self._session_fulldebug:
                _LOGGER.debug(f'Using login action url: "{pw_url}"')
            #url = 'https://b-h-s.spr.us00.p.con-veh.net/oidc/v1/authorize'
            req = await self._session.post(
                url=pw_url, headers=self._session_auth_headers, data=pw_form, allow_redirects=False
                #url=url, headers=self._session_auth_headers, data=pw_form, allow_redirects=False
            )
            _LOGGER.debug("Parsing login response.")
            # Follow all redirects until we get redirected back to "our app"
            try:
                max_depth = 10
                ref = urljoin(pw_url, req.headers["Location"])
                while not ref.startswith(APP_URI):
                    if self._session_fulldebug:
                        _LOGGER.debug(f'Following redirect to "{ref}"')
                    response = await self._session.get(
                        url=ref, headers=self._session_auth_headers, allow_redirects=False
                    )
                    if not response.headers.get("Location", False):
                        _LOGGER.info("Login failed, does this account have any vehicle with connect services enabled?")
                        raise Exception("User appears unauthorized")
                    ref = urljoin(ref, response.headers["Location"])
                    # Set a max limit on requests to prevent forever loop
                    max_depth -= 1
                    if max_depth == 0:
                        _LOGGER.warning("Should have gotten a token by now.")
                        raise Exception("Too many redirects")
            except Exception as e:
                # If we get excepted it should be because we can't redirect to the APP_URI URL
                if "error" in ref:
                    error_msg = parse_qs(urlparse(ref).query).get("error", "")[0]
                    if error_msg == "login.error.throttled":
                        timeout = parse_qs(urlparse(ref).query).get("enableNextButtonAfterSeconds", "")[0]
                        _LOGGER.warning(f"Login failed, login is disabled for another {timeout} seconds")
                    elif error_msg == "login.errors.password_invalid":
                        _LOGGER.warning("Login failed, invalid password")
                    else:
                        _LOGGER.warning(f"Login failed: {error_msg}")
                    raise AuthenticationException(error_msg)
                if "code" in ref:
                    _LOGGER.debug("Got code: %s" % ref)
                else:
                    _LOGGER.debug("Exception occurred while logging in.")
                    raise e
            _LOGGER.debug("Login successful, received authorization code.")

            # Extract code and tokens

            try:
                parsed_qs = parse_qs(urlparse(ref).fragment)
                jwt_auth_code = parsed_qs["code"][0]
                jwt_id_token = parsed_qs["id_token"][0]
                jwt_ui_locales = ""
            except Exception as error:
                ref = ref.replace('///','//')
                parsed_qs = parse_qs((urlparse(ref)[4]))
                jwt_auth_code = parsed_qs["code"][0]
                jwt_id_token = parsed_qs["state"][0]
                jwt_ui_locales = parsed_qs["ui_locales"][0]

            # Exchange Auth code and id_token for new tokens with refresh_token (so we can easier fetch new ones later)
            if COUNTRY == 'DE':
                token_body = {
                    "auth_code": jwt_auth_code,
                    "id_token": jwt_id_token,
                    "code_verifier": code_verifier.decode(),
                    "brand": BRAND,
                }
            else:
                token_body = {
                    "state": jwt_auth_code,
                    "code": jwt_auth_code,
                    "ui_locales": jwt_ui_locales
                }
            _LOGGER.debug("Trying to fetch user identity tokens.")
            token_url = "https://tokenrefreshservice.apps.emea.vwapps.io/exchangeAuthCode"
            req = await self._session.post(
                url=token_url, headers=self._session_auth_headers, data=token_body, allow_redirects=False
            )
            if req.status != 200:
                raise Exception("Token exchange failed")
            # Save tokens as "identity", these are tokens representing the user
            self._session_tokens[client] = await req.json()
            if "error" in self._session_tokens[client]:
                error_msg = self._session_tokens[client].get("error", "")
                if "error_description" in self._session_tokens[client]:
                    error_description = self._session_tokens[client].get("error_description", "")
                    raise Exception(f"{error_msg} - {error_description}")
                else:
                    raise Exception(error_msg)
            if self._session_fulldebug:
                for token in self._session_tokens.get(client, {}):
                    _LOGGER.debug(f"Got token {token}")
            if not await self.verify_tokens(self._session_tokens[client].get("id_token", ""), "identity"):
                _LOGGER.warning("User identity token could not be verified!")
            else:
                _LOGGER.debug("User identity token verified OK.")
        except Exception as error:
            _LOGGER.error(f"Login failed for {BRAND} account, {error}")
            _LOGGER.exception(error)
            self._session_logged_in = False
            return False
        return True

    async def _getAPITokens(self):
        try:
            # Get VW Group API tokens
            # https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/token
            tokenBody2 = {
                "grant_type": "id_token",
                "token": self._session_tokens["identity"]["id_token"],
                "scope": "sc2:fal",
            }
            _LOGGER.debug("Trying to fetch api tokens.")
            req = await self._session.post(
                url="https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/token",
                headers={
                    "User-Agent": USER_AGENT,
                    "X-App-Version": XAPPVERSION,
                    "X-App-Name": XAPPNAME,
                    "X-Client-Id": XCLIENT_ID,
                },
                data=tokenBody2,
                allow_redirects=False,
            )
            if req.status > 400:
                _LOGGER.debug("API token request failed.")
                raise Exception(f"API token request returned with status code {req.status}")
            else:
                # Save tokens as "vwg", use these for get/posts to VW Group API
                self._session_tokens["vwg"] = await req.json()
                if "error" in self._session_tokens["vwg"]:
                    error = self._session_tokens["vwg"].get("error", "")
                    if "error_description" in self._session_tokens["vwg"]:
                        error_description = self._session_tokens["vwg"].get("error_description", "")
                        raise Exception(f"{error} - {error_description}")
                    else:
                        raise Exception(error)
                if self._session_fulldebug:
                    for token in self._session_tokens.get("vwg", {}):
                        _LOGGER.debug(f"Got token {token}")
                if not await self.verify_tokens(self._session_tokens["vwg"].get("access_token", ""), "vwg"):
                    _LOGGER.warning("VW-Group API token could not be verified!")
                else:
                    _LOGGER.debug("VW-Group API token verified OK.")

            # Update headers for requests, defaults to using VWG token
            self._session_headers["Authorization"] = "Bearer " + self._session_tokens["vwg"]["access_token"]
        except Exception as error:
            _LOGGER.error(f"Failed to fetch VW-Group API tokens, {error}")
            self._session_logged_in = False
            return False
        return True

    async def terminate(self):
        """Log out from connect services."""
        _LOGGER.info("Initiating logout")
        await self.logout()

    async def logout(self):
        """Logout, revoke tokens."""
        self._session_headers.pop("Authorization", None)

        if self._session_logged_in:
            if self._session_headers.get("vwg", {}).get("access_token"):
                _LOGGER.info("Revoking API Access Token...")
                self._session_headers["token_type_hint"] = "access_token"
                params = {"token": self._session_tokens["vwg"]["access_token"]}
                await self.post(
                    "https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/revoke", data=params
                )
            if self._session_headers.get("vwg", {}).get("refresh_token"):
                _LOGGER.info("Revoking API Refresh Token...")
                self._session_headers["token_type_hint"] = "refresh_token"
                params = {"token": self._session_tokens["vwg"]["refresh_token"]}
                await self.post(
                    "https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/revoke", data=params
                )
                self._session_headers.pop("token_type_hint", None)
            if self._session_headers.get("identity", {}).get("identity_token"):
                _LOGGER.info("Revoking Identity Access Token...")
                # params = {
                #    "token": self._session_tokens['identity']['access_token'],
                #    "brand": BRAND
                # }
                # revoke_at = await self.post('https://tokenrefreshservice.apps.emea.vwapps.io/revokeToken', data = params)
            if self._session_headers.get("identity", {}).get("refresh_token"):
                _LOGGER.info("Revoking Identity Refresh Token...")
                params = {"token": self._session_tokens["identity"]["refresh_token"], "brand": BRAND}
                await self.post("https://tokenrefreshservice.apps.emea.vwapps.io/revokeToken", data=params)

    # HTTP methods to API
    async def _request(self, method, url, **kwargs):
        """Perform a query to the VW-Group API."""
        _LOGGER.debug(f'HTTP {method} "{url}"')
        async with self._session.request(
            method,
            url,
            headers=self._session_headers,
            timeout=ClientTimeout(total=TIMEOUT.seconds),
            cookies=self._jarCookie,
            raise_for_status=False,
            **kwargs,
        ) as response:
            response.raise_for_status()

            # Update cookie jar
            if self._jarCookie != "":
                self._jarCookie.update(response.cookies)
            else:
                self._jarCookie = response.cookies

            try:
                if response.status == 204:
                    res = {"status_code": response.status}
                elif response.status >= 200 or response.status <= 300:
                    res = await response.json(loads=json_loads)
                else:
                    res = {}
                    _LOGGER.debug(f"Not success status code [{response.status}] response: {response}")
                if "X-RateLimit-Remaining" in response.headers:
                    res["rate_limit_remaining"] = response.headers.get("X-RateLimit-Remaining", "")
            except Exception:
                res = {}
                _LOGGER.debug(f"Something went wrong [{response.status}] response: {response}")
                return res

            if self._session_fulldebug:
                _LOGGER.debug(f'Request for "{url}" returned with status code [{response.status}], response: {res}')
            else:
                _LOGGER.debug(f'Request for "{url}" returned with status code [{response.status}]')
            return res

    async def get(self, url, vin="", tries=0):
        """Perform a get query."""
        try:
            response = await self._request(METH_GET, self._make_url(url, vin))
            return response
        except client_exceptions.ClientResponseError as error:
            if error.status == 400:
                _LOGGER.error(
                    'Got HTTP 400 "Bad Request" from server, this request might be malformed or not implemented'
                    " correctly for this vehicle"
                )
            elif error.status == 401:
                _LOGGER.warning(f'Received "unauthorized" error while fetching data: {error}')
                self._session_logged_in = False
            elif error.status == 429 and tries < MAX_RETRIES_ON_RATE_LIMIT:
                delay = randint(1, 3 + tries * 2)
                _LOGGER.debug(f"Server side throttled. Waiting {delay}, try {tries + 1}")
                await asyncio.sleep(delay)
                return await self.get(url, vin, tries + 1)
            elif error.status == 500:
                _LOGGER.info("Got HTTP 500 from server, service might be temporarily unavailable")
            elif error.status == 502:
                _LOGGER.info("Got HTTP 502 from server, this request might not be supported for this vehicle")
            else:
                _LOGGER.error(f"Got unhandled error from server: {error.status}")
            return {"status_code": error.status}

    async def post(self, url, vin="", tries=0, **data):
        """Perform a post query."""
        try:
            if data:
                return await self._request(METH_POST, self._make_url(url, vin), **data)
            else:
                return await self._request(METH_POST, self._make_url(url, vin))
        except client_exceptions.ClientResponseError as error:
            if error.status == 429 and tries < MAX_RETRIES_ON_RATE_LIMIT:
                delay = randint(1, 3 + tries * 2)
                _LOGGER.debug(f"Server side throttled. Waiting {delay}, try {tries + 1}")
                await asyncio.sleep(delay)
                return await self.post(url, vin, tries + 1, **data)

    # Construct URL from request, home region and variables
    def _make_url(self, ref, vin=""):
        replacedUrl = re.sub("\\$vin", vin, ref)
        if "://" in replacedUrl:
            # already server contained in URL
            return replacedUrl
        elif "rolesrights" in replacedUrl:
            return urljoin(self._session_spin_ref_urls[vin], replacedUrl)
        else:
            return urljoin(self._session_auth_ref_urls[vin], replacedUrl)

    # Update data for all Vehicles
    async def update(self):
        """Update status."""
        if not self.logged_in:
            if not await self._login():
                _LOGGER.warning(f"Login for {BRAND} account failed!")
                return False
        try:
            if not await self.validate_tokens:
                _LOGGER.info(f"Session expired. Initiating new login for {BRAND} account.")
                if not await self.doLogin():
                    _LOGGER.warning(f"Login for {BRAND} account failed!")
                    raise Exception(f"Login for {BRAND} account failed")

            _LOGGER.debug("Going to call vehicle updates")
            # Get all Vehicle objects and update in parallell
            updatelist = []
            for vehicle in self.vehicles:
                updatelist.append(vehicle.update())
            # Wait for all data updates to complete
            await asyncio.gather(*updatelist)

            return True
        except (OSError, LookupError, Exception) as error:
            _LOGGER.warning(f"Could not update information: {error}")
        return False

    # Data collect functions #
    async def getHomeRegion(self, vin):
        """Get API requests base url for VIN."""
        if not await self.validate_tokens:
            return False
        try:
            await self.set_token("vwg")
            response = await self.get(
                "https://mal-1a.prd.ece.vwg-connect.com/api/cs/vds/v1/vehicles/$vin/homeRegion", vin
            )
            self._session_auth_ref_urls[vin] = (
                response["homeRegion"]["baseUri"]["content"].split("/api")[0].replace("mal-", "fal-")
                if response["homeRegion"]["baseUri"]["content"] != "https://mal-1a.prd.ece.vwg-connect.com/api"
                else "https://msg.volkswagen.de"
            )
            self._session_spin_ref_urls[vin] = response["homeRegion"]["baseUri"]["content"].split("/api")[0]
            return response["homeRegion"]["baseUri"]["content"]
        except Exception as error:
            _LOGGER.debug(f"Could not get homeregion, error {error}")
            self._session_logged_in = False
        return False

    async def getOperationList(self, vin):
        """Collect operationlist for VIN, supported/licensed functions."""
        if not await self.validate_tokens:
            return False
        try:
            await self.set_token("vwg")
            response = await self.get("/api/rolesrights/operationlist/v3/vehicles/$vin", vin)
            if response.get("operationList", False):
                data = response.get("operationList", {})
            elif response.get("status_code", {}):
                _LOGGER.warning(f'Could not fetch operation list, HTTP status code: {response.get("status_code")}')
                data = response
            else:
                _LOGGER.info(f"Could not fetch operation list: {response}")
                data = {"error": "unknown"}
        except Exception as error:
            _LOGGER.warning(f"Could not fetch operation list, error: {error}")
            data = {"error": "unknown"}
        return data

    async def getRealCarData(self, vin):
        """Get car information from customer profile, VIN, nickname, etc."""
        if not await self.validate_tokens:
            return False
        try:
            _LOGGER.debug("Attempting extraction of subject from identity token.")
            atoken = self._session_tokens["identity"]["access_token"]
            subject = jwt.decode(atoken, options={"verify_signature": False}, algorithms=JWT_ALGORITHMS).get(
                "sub", None
            )
            await self.set_token("identity")
            self._session_headers["Accept"] = "application/json"
            response = await self.get(f"https://customer-profile.vwgroup.io/v1/customers/{subject}/realCarData")
            if response.get("realCars", {}):
                data = {
                    "carData": next(
                        item for item in response.get("realCars", []) if item["vehicleIdentificationNumber"] == vin
                    )
                }
                return data
            elif response.get("status_code", {}):
                _LOGGER.warning(f'Could not fetch realCarData, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info("Unhandled error while trying to fetch realcar data")
        except Exception as error:
            _LOGGER.warning(f"Could not fetch realCarData, error: {error}")
        return False

    async def getCarportData(self, vin):
        """Get carport data for vehicle, model, model year etc."""
        if not await self.validate_tokens:
            return False
        try:
            await self.set_token("vwg")
            self._session_headers["Accept"] = (
                "application/vnd.vwg.mbb.vehicleDataDetail_v2_1_0+json,"
                " application/vnd.vwg.mbb.genericError_v1_0_2+json"
            )
            response = await self.get(
                f"fs-car/vehicleMgmt/vehicledata/v2/{BRAND}/{self._session_country}/vehicles/$vin", vin=vin
            )
            self._session_headers["Accept"] = "application/json"

            if response.get("vehicleDataDetail", {}).get("carportData", {}):
                data = {"carportData": response.get("vehicleDataDetail", {}).get("carportData", {})}
                return data
            elif response.get("status_code", {}):
                _LOGGER.warning(f'Could not fetch carportdata, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info("Unhandled error while trying to fetch carport data")
        except Exception as error:
            _LOGGER.warning(f"Could not fetch carportData, error: {error}")
        return False

    async def getVehicleStatusData(self, vin):
        """Get stored vehicle data response."""
        try:
            await self.set_token("vwg")
            response = await self.get(f"fs-car/bs/vsr/v1/{BRAND}/{self._session_country}/vehicles/$vin/status", vin=vin)
            if (
                response.get("StoredVehicleDataResponse", {})
                .get("vehicleData", {})
                .get("data", {})[0]
                .get("field", {})[0]
            ):
                data = {
                    "StoredVehicleDataResponse": response.get("StoredVehicleDataResponse", {}),
                    "StoredVehicleDataResponseParsed": {
                        e["id"]: e if "value" in e else ""
                        for f in [s["field"] for s in response["StoredVehicleDataResponse"]["vehicleData"]["data"]]
                        for e in f
                    },
                }
                return data
            elif response.get("status_code", {}):
                _LOGGER.warning(
                    f'Could not fetch vehicle status report, HTTP status code: {response.get("status_code")}'
                )
            else:
                _LOGGER.info("Unhandled error while trying to fetch status data")
        except Exception as error:
            _LOGGER.warning(f"Could not fetch StoredVehicleDataResponse, error: {error}")
        return False

    async def getTripStatistics(self, vin):
        """Get short term trip statistics."""
        if not await self.validate_tokens:
            return False
        try:
            await self.set_token("vwg")
            response = await self.get(
                f"fs-car/bs/tripstatistics/v1/{BRAND}/{self._session_country}/vehicles/$vin/tripdata/shortTerm?newest",
                vin=vin,
            )
            if response.get("tripData", {}):
                data = {"tripstatistics": response.get("tripData", {})}
                return data
            elif response.get("status_code", {}):
                _LOGGER.warning(f'Could not fetch trip statistics, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info("Unhandled error while trying to fetch trip statistics")
        except Exception as error:
            _LOGGER.warning(f"Could not fetch trip statistics, error: {error}")
        return False

    async def getPosition(self, vin):
        """Get position data."""
        if not await self.validate_tokens:
            return False
        try:
            await self.set_token("vwg")
            response = await self.get(
                f"fs-car/bs/cf/v1/{BRAND}/{self._session_country}/vehicles/$vin/position", vin=vin
            )
            if response.get("findCarResponse", {}):
                data = {"findCarResponse": response.get("findCarResponse", {}), "isMoving": False}
                return data
            elif response.get("status_code", {}):
                if response.get("status_code", 0) == 204:
                    _LOGGER.debug("Seems car is moving, HTTP 204 received from position")
                    data = {"isMoving": True, "rate_limit_remaining": 15}
                    return data
                else:
                    _LOGGER.warning(f'Could not fetch position, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info("Unhandled error while trying to fetch positional data")
        except Exception as error:
            _LOGGER.warning(f"Could not fetch position, error: {error}")
        return False

    async def getTimers(self, vin) -> TimerData | None:
        """Get departure timers."""
        if not await self.validate_tokens:
            return None
        try:
            await self.set_token("vwg")
            response = await self.get(
                f"fs-car/bs/departuretimer/v1/{BRAND}/{self._session_country}/vehicles/$vin/timer", vin=vin
            )
            timer = TimerData(**(response.get("timer", {})))
            if timer.valid:
                return timer
            elif response.get("status_code", {}):
                _LOGGER.warning(f'Could not fetch timers, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info("Unknown error while trying to fetch data for departure timers")
        except Exception as error:
            _LOGGER.warning(f"Could not fetch timers, error: {error}")
        return None

    async def getClimater(self, vin):
        """Get climatisation data."""
        if not await self.validate_tokens:
            return False
        try:
            await self.set_token("vwg")
            response = await self.get(
                f"fs-car/bs/climatisation/v1/{BRAND}/{self._session_country}/vehicles/$vin/climater", vin=vin
            )
            if response.get("climater", {}):
                data = {"climater": response.get("climater", {})}
                return data
            elif response.get("status_code", {}):
                _LOGGER.warning(f'Could not fetch climatisation, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info("Unhandled error while trying to fetch climatisation data")
        except Exception as error:
            _LOGGER.warning(f"Could not fetch climatisation, error: {error}")
        return False

    async def getCharger(self, vin):
        """Get charger data."""
        if not await self.validate_tokens:
            return False
        try:
            await self.set_token("vwg")
            response = await self.get(
                f"fs-car/bs/batterycharge/v1/{BRAND}/{self._session_country}/vehicles/$vin/charger", vin=vin
            )
            if response.get("charger", {}):
                data = {"charger": response.get("charger", {})}
                return data
            elif response.get("status_code", {}):
                _LOGGER.warning(f'Could not fetch pre-heating, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info("Unhandled error while trying to fetch charger data")
        except Exception as error:
            _LOGGER.warning(f"Could not fetch charger, error: {error}")
        return False

    async def getPreHeater(self, vin):
        """Get parking heater data."""
        if not await self.validate_tokens:
            return False
        try:
            await self.set_token("vwg")
            response = await self.get(f"fs-car/bs/rs/v1/{BRAND}/{self._session_country}/vehicles/$vin/status", vin=vin)
            if response.get("statusResponse", {}):
                data = {"heating": response.get("statusResponse", {})}
                return data
            elif response.get("status_code", {}):
                _LOGGER.warning(f'Could not fetch pre-heating, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info("Unhandled error while trying to fetch pre-heating data")
        except Exception as error:
            _LOGGER.warning(f"Could not fetch pre-heating, error: {error}")
        return False

    async def get_request_status(self, vin, sectionId, requestId):
        """Return status of a request ID for a given section ID."""
        if self.logged_in is False:
            if not await self.doLogin():
                _LOGGER.warning(f"Login for {BRAND} account failed!")
                raise Exception(f"Login for {BRAND} account failed")
        try:
            if not await self.validate_tokens:
                _LOGGER.info(f"Session expired. Initiating new login for {BRAND} account.")
                if not await self.doLogin():
                    _LOGGER.warning(f"Login for {BRAND} account failed!")
                    raise Exception(f"Login for {BRAND} account failed")
            await self.set_token("vwg")
            if sectionId == "climatisation":
                url = (
                    f"fs-car/bs/$sectionId/v1/{BRAND}/{self._session_country}/vehicles/$vin/climater/actions/$requestId"
                )
            elif sectionId == "batterycharge":
                url = (
                    f"fs-car/bs/$sectionId/v1/{BRAND}/{self._session_country}/vehicles/$vin/charger/actions/$requestId"
                )
            elif sectionId == "departuretimer":
                url = f"fs-car/bs/$sectionId/v1/{BRAND}/{self._session_country}/vehicles/$vin/timer/actions/$requestId"
            elif sectionId in ["vsr", "refresh"]:
                url = f"fs-car/bs/vsr/v1/{BRAND}/{self._session_country}/vehicles/$vin/requests/$requestId/jobstatus"
            else:
                url = (
                    f"fs-car/bs/$sectionId/v1/{BRAND}/{self._session_country}/vehicles/$vin/requests/$requestId/status"
                )
            url = re.sub("\\$sectionId", sectionId, url)
            url = re.sub("\\$requestId", requestId, url)

            response = await self.get(url, vin)
            # Pre-heater, ???
            if response.get("requestStatusResponse", {}).get("status", False):
                result = response.get("requestStatusResponse", {}).get("status", False)
            # For electric charging, climatisation and departure timers
            elif response.get("action", {}).get("actionState", False):
                result = response.get("action", {}).get("actionState", False)
            else:
                result = "Unknown"
            # Translate status messages to meaningful info
            if result == "request_in_progress" or result == "queued" or result == "fetched":
                status = "In progress"
            elif result == "request_fail" or result == "failed":
                status = "Failed"
            elif result == "unfetched":
                status = "No response"
            elif result == "request_successful" or result == "succeeded":
                status = "Success"
            else:
                status = result
            return status
        except Exception as error:
            _LOGGER.warning(f"Failure during get request status: {error}")
            raise Exception(f"Failure during get request status: {error}")

    async def get_sec_token(self, vin, spin, action):
        """Get a security token, required for certain set functions."""
        urls = {
            "lock": "/api/rolesrights/authorization/v2/vehicles/$vin/services/rlu_v1/operations/LOCK/security-pin-auth-requested",
            "unlock": "/api/rolesrights/authorization/v2/vehicles/$vin/services/rlu_v1/operations/UNLOCK/security-pin-auth-requested",
            "heating": "/api/rolesrights/authorization/v2/vehicles/$vin/services/rheating_v1/operations/P_QSACT/security-pin-auth-requested",
            "timer": "/api/rolesrights/authorization/v2/vehicles/$vin/services/timerprogramming_v1/operations/P_SETTINGS_AU/security-pin-auth-requested",
            "rclima": "/api/rolesrights/authorization/v2/vehicles/$vin/services/rclima_v1/operations/P_START_CLIMA_AU/security-pin-auth-requested",
        }
        if not spin:
            raise Exception("SPIN is required")
        try:
            if not urls.get(action, False):
                raise Exception(f'Security token for "{action}" is not implemented')
            response = await self.get(self._make_url(urls.get(action), vin=vin))
            secToken = response["securityPinAuthInfo"]["securityToken"]
            challenge = response["securityPinAuthInfo"]["securityPinTransmission"]["challenge"]
            spinHash = self.hash_spin(challenge, spin)
            body = {
                "securityPinAuthentication": {
                    "securityPin": {"challenge": challenge, "securityPinHash": spinHash},
                    "securityToken": secToken,
                }
            }
            self._session_headers["Content-Type"] = "application/json"
            response = await self.post(
                self._make_url("/api/rolesrights/authorization/v2/security-pin-auth-completed", vin=vin), json=body
            )
            self._session_headers.pop("Content-Type", None)
            if response.get("securityToken", False):
                return response["securityToken"]
            else:
                _LOGGER.warning("Did not receive a valid security token")
                raise Exception("Did not receive a valid security token")
        except Exception as error:
            _LOGGER.error(f"Could not generate security token (maybe wrong SPIN?), error: {error}")
            raise

    # Data set functions #
    async def dataCall(self, query, vin="", **data):
        """Execute actions through VW-Group API."""
        if self.logged_in is False:
            if not await self.doLogin():
                _LOGGER.warning(f"Login for {BRAND} account failed!")
                raise Exception(f"Login for {BRAND} account failed")
        try:
            if not await self.validate_tokens:
                _LOGGER.info(f"Session expired. Initiating new login for {BRAND} account.")
                if not await self.doLogin():
                    _LOGGER.warning(f"Login for {BRAND} account failed!")
                    raise Exception(f"Login for {BRAND} account failed")
            response = await self.post(query, vin=vin, **data)
            _LOGGER.debug(f"Data call returned: {response}")
            return response
        except client_exceptions.ClientResponseError as error:
            if error.status == 401:
                _LOGGER.error("Unauthorized")
                self._session_logged_in = False
            elif error.status == 400:
                _LOGGER.error("Bad request")
            elif error.status == 429:
                _LOGGER.warning(
                    "Too many requests. Further requests can only be made after the end of next trip in order to"
                    " protect your vehicles battery."
                )
                return 429
            elif error.status == 500:
                _LOGGER.error("Internal server error, server might be temporarily unavailable")
            elif error.status == 502:
                _LOGGER.error("Bad gateway, this function may not be implemented for this vehicle")
            else:
                _LOGGER.error(f"Unhandled HTTP exception: {error}")
            # return False
        except Exception as error:
            _LOGGER.error(f"Failure to execute: {error}")
        return False

    async def setRefresh(self, vin):
        """Force vehicle data update."""
        try:
            await self.set_token("vwg")
            response = await self.dataCall(
                f"fs-car/bs/vsr/v1/{BRAND}/{self._session_country}/vehicles/$vin/requests", vin, data=None
            )
            if not response:
                raise Exception("Invalid or no response")
            elif response == 429:
                return dict({"id": None, "state": "Throttled", "rate_limit_remaining": 0})
            else:
                request_id = response.get("CurrentVehicleDataResponse", {}).get("requestId", 0)
                request_state = response.get("CurrentVehicleDataResponse", {}).get("requestState", "queued")
                remaining = response.get("rate_limit_remaining", -1)
                _LOGGER.debug(
                    f'Request to refresh data returned with state "{request_state}", request id: {request_id},'
                    f" remaining requests: {remaining}"
                )
                return dict({"id": str(request_id), "state": request_state, "rate_limit_remaining": remaining})
        except:
            raise

    async def setCharger(self, vin, data) -> dict[str, str | int | None]:
        """Start/Stop charger."""
        try:
            await self.set_token("vwg")
            response = await self.dataCall(
                f"fs-car/bs/batterycharge/v1/{BRAND}/{self._session_country}/vehicles/$vin/charger/actions",
                vin,
                json=data,
            )
            if not response:
                raise Exception("Invalid or no response")
            elif response == 429:
                return dict({"id": None, "state": "Throttled", "rate_limit_remaining": 0})
            else:
                request_id = response.get("action", {}).get("actionId", 0)
                request_state = response.get("action", {}).get("actionState", "unknown")
                remaining = response.get("rate_limit_remaining", -1)
                _LOGGER.debug(
                    f'Request for charger action returned with state "{request_state}", request id: {request_id},'
                    f" remaining requests: {remaining}"
                )
                return dict({"id": str(request_id), "state": request_state, "rate_limit_remaining": remaining})
        except:
            raise

    async def setClimater(self, vin, data, spin):
        """Execute climatisation actions."""
        try:
            await self.set_token("vwg")
            # Only get security token if auxiliary heater is to be started
            if data.get("action", {}).get("settings", {}).get("heaterSource", None) == "auxiliary":
                self._session_headers["X-securityToken"] = await self.get_sec_token(vin=vin, spin=spin, action="rclima")
            response = await self.dataCall(
                f"fs-car/bs/climatisation/v1/{BRAND}/{self._session_country}/vehicles/$vin/climater/actions",
                vin,
                json=data,
            )
            self._session_headers.pop("X-securityToken", None)
            if not response:
                raise Exception("Invalid or no response")
            elif response == 429:
                return dict({"id": None, "state": "Throttled", "rate_limit_remaining": 0})
            else:
                request_id = response.get("action", {}).get("actionId", 0)
                request_state = response.get("action", {}).get("actionState", "unknown")
                remaining = response.get("rate_limit_remaining", -1)
                _LOGGER.debug(
                    f'Request for climater action returned with state "{request_state}", request id: {request_id},'
                    f" remaining requests: {remaining}"
                )
                return dict({"id": str(request_id), "state": request_state, "rate_limit_remaining": remaining})
        except:
            self._session_headers.pop("X-securityToken", None)
            raise

    async def setPreHeater(self, vin, data, spin):
        """Petrol/diesel parking heater actions."""
        content_type = None
        try:
            await self.set_token("vwg")
            if "Content-Type" in self._session_headers:
                content_type = self._session_headers["Content-Type"]
            else:
                content_type = ""
            self._session_headers["Content-Type"] = "application/vnd.vwg.mbb.RemoteStandheizung_v2_0_2+json"
            if "quickstop" not in data:
                self._session_headers["x-mbbSecToken"] = await self.get_sec_token(vin=vin, spin=spin, action="heating")
            response = await self.dataCall(
                f"fs-car/bs/rs/v1/{BRAND}/{self._session_country}/vehicles/$vin/action", vin=vin, json=data
            )
            # Clean up headers
            self._session_headers.pop("x-mbbSecToken", None)
            self._session_headers.pop("Content-Type", None)
            if content_type:
                self._session_headers["Content-Type"] = content_type

            if not response:
                raise Exception("Invalid or no response")
            elif response == 429:
                return dict({"id": None, "state": "Throttled", "rate_limit_remaining": 0})
            else:
                request_id = response.get("performActionResponse", {}).get("requestId", 0)
                remaining = response.get("rate_limit_remaining", -1)
                _LOGGER.debug(
                    f"Request for parking heater is queued with request id: {request_id}, remaining requests:"
                    f" {remaining}"
                )
                return dict({"id": str(request_id), "state": None, "rate_limit_remaining": remaining})
        except Exception:
            self._session_headers.pop("x-mbbSecToken", None)
            self._session_headers.pop("Content-Type", None)
            if content_type:
                self._session_headers["Content-Type"] = content_type
            raise

    async def setTimersAndProfiles(self, vin, data: TimersAndProfiles):
        """Set schedules."""
        return await self._setDepartureTimer(vin, data, "setTimersAndProfiles")

    async def setChargeMinLevel(self, vin: str, limit: int):
        """Set schedules."""
        data: TimerData | None = await self.getTimers(vin)
        if data is None or data.timersAndProfiles is None or data.timersAndProfiles.timerBasicSetting is None:
            raise Exception("No existing timer data?")
        data.timersAndProfiles.timerBasicSetting.set_charge_min_limit(limit)
        return await self._setDepartureTimer(vin, data.timersAndProfiles, "setChargeMinLimit")

    # Not working :/
    # async def setHeaterSource(self, vin: str, source: str):
    #     """Set heater source for departure timers."""
    #     data: Optional[TimerData] = await self.getTimers(vin)
    #     if data is None:
    #         raise Exception("No existing timer data?")
    #     data.timersAndProfiles.timerBasicSetting.set_heater_source(source)
    #     return await self._setDepartureTimer(vin, data.timersAndProfiles, "setHeaterSource")

    async def _setDepartureTimer(self, vin, data: TimersAndProfiles, action: str):
        """Set schedules."""
        try:
            await self.set_token("vwg")
            response = await self.dataCall(
                f"fs-car/bs/departuretimer/v1/{BRAND}/{self._session_country}/vehicles/$vin/timer/actions",
                vin=vin,
                json={
                    "action": {
                        "timersAndProfiles": data.json_updated["timer"],
                        "type": action,
                    }
                },
            )

            self._session_headers.pop("X-securityToken", None)
            if not response:
                raise Exception("Invalid or no response")
            elif response == 429:
                return dict({"id": None, "state": "Throttled", "rate_limit_remaining": 0})
            else:
                request_id = response.get("action", {}).get("actionId", 0)
                request_state = response.get("action", {}).get("actionState", "unknown")
                remaining = response.get("rate_limit_remaining", -1)
                _LOGGER.debug(
                    f'Request for timer action returned with state "{request_state}", request id: {request_id},'
                    f" remaining requests: {remaining}"
                )
                return dict({"id": str(request_id), "state": request_state, "rate_limit_remaining": remaining})
        except:
            self._session_headers.pop("X-securityToken", None)
            raise

    async def setLock(self, vin, data, spin):
        """Remote lock and unlock actions."""
        content_type = None
        try:
            await self.set_token("vwg")
            # Prepare data, headers and fetch security token
            if "Content-Type" in self._session_headers:
                content_type = self._session_headers["Content-Type"]
            else:
                content_type = ""
            if "unlock" in data:
                self._session_headers["X-mbbSecToken"] = await self.get_sec_token(vin=vin, spin=spin, action="unlock")
            else:
                self._session_headers["X-mbbSecToken"] = await self.get_sec_token(vin=vin, spin=spin, action="lock")
            self._session_headers["Content-Type"] = "application/vnd.vwg.mbb.RemoteLockUnlock_v1_0_0+xml"
            response = await self.dataCall(
                f"fs-car/bs/rlu/v1/{BRAND}/{self._session_country}/vehicles/$vin/actions", vin, data=data
            )
            # Clean up headers
            self._session_headers.pop("X-mbbSecToken", None)
            self._session_headers.pop("Content-Type", None)
            if content_type:
                self._session_headers["Content-Type"] = content_type
            if not response:
                raise Exception("Invalid or no response")
            elif response == 429:
                return dict({"id": None, "state": "Throttled", "rate_limit_remaining": 0})
            else:
                request_id = response.get("rluActionResponse", {}).get("requestId", 0)
                request_state = response.get("rluActionResponse", {}).get("requestId", "unknown")
                remaining = response.get("rate_limit_remaining", -1)
                _LOGGER.debug(
                    f'Request for lock action returned with state "{request_state}", request id: {request_id},'
                    f" remaining requests: {remaining}"
                )
                return dict({"id": str(request_id), "state": request_state, "rate_limit_remaining": remaining})
        except:
            self._session_headers.pop("X-mbbSecToken", None)
            self._session_headers.pop("Content-Type", None)
            if content_type:
                self._session_headers["Content-Type"] = content_type
            raise

    # Token handling #
    @property
    async def validate_tokens(self):
        """Validate expiry of tokens."""
        idtoken = self._session_tokens["identity"]["id_token"]
        atoken = self._session_tokens["vwg"]["access_token"]
        id_exp = jwt.decode(
            idtoken, options={"verify_signature": False, "verify_aud": False}, algorithms=JWT_ALGORITHMS
        ).get("exp", None)
        at_exp = jwt.decode(
            atoken, options={"verify_signature": False, "verify_aud": False}, algorithms=JWT_ALGORITHMS
        ).get("exp", None)
        id_dt = datetime.fromtimestamp(int(id_exp))
        at_dt = datetime.fromtimestamp(int(at_exp))
        now = datetime.now()
        later = now + self._session_refresh_interval

        # Check if tokens have expired, or expires now
        if now >= id_dt or now >= at_dt:
            _LOGGER.debug("Tokens have expired. Try to fetch new tokens.")
            if await self.refresh_tokens():
                _LOGGER.debug("Successfully refreshed tokens")
            else:
                return False
        # Check if tokens expires before next update
        elif later >= id_dt or later >= at_dt:
            _LOGGER.debug("Tokens about to expire. Try to fetch new tokens.")
            if await self.refresh_tokens():
                _LOGGER.debug("Successfully refreshed tokens")
            else:
                return False
        return True

    async def verify_tokens(self, token, type, client="Legacy"):
        """Verify JWT against JWK(s)."""
        if type == "identity":
            req = await self._session.get(url="https://identity.vwgroup.io/oidc/v1/keys")
            keys = await req.json()
            audience = [
                CLIENT[client].get("CLIENT_ID"),
                "VWGMBB01DELIV1",
                "https://api.vas.eu.dp15.vwg-connect.com",
                "https://api.vas.eu.wcardp.io",
            ]
        elif type == "vwg":
            req = await self._session.get(url="https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/public/jwk/v1")
            keys = await req.json()
            audience = "mal.prd.ece.vwg-connect.com"
        else:
            _LOGGER.debug("Not implemented")
            return False
        try:
            pubkeys = {}
            for jwk in keys["keys"]:
                kid = jwk["kid"]
                if jwk["kty"] == "RSA":
                    pubkeys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(to_json(jwk))

            token_kid = jwt.get_unverified_header(token)["kid"]
            if type == "vwg":
                token_kid = "VWGMBB01DELIV1." + token_kid

            pubkey = pubkeys[token_kid]
            jwt.decode(token, key=pubkey, algorithms=JWT_ALGORITHMS, audience=audience)
            return True
        except Exception as error:
            _LOGGER.debug(f"Failed to verify token, error: {error}")
            return False

    async def refresh_tokens(self):
        """Refresh tokens."""
        try:
            tHeaders = {
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
                "X-App-Version": XAPPVERSION,
                "X-App-Name": XAPPNAME,
                "X-Client-Id": XCLIENT_ID,
            }

            body = {
                "grant_type": "refresh_token",
                "brand": BRAND,
                "refresh_token": self._session_tokens["identity"]["refresh_token"],
            }
            response = await self._session.post(
                url="https://tokenrefreshservice.apps.emea.vwapps.io/refreshTokens", headers=tHeaders, data=body
            )
            if response.status == 200:
                tokens = await response.json()
                # Verify Token
                if not await self.verify_tokens(tokens["id_token"], "identity"):
                    _LOGGER.warning("Token could not be verified!")
                for token in tokens:
                    self._session_tokens["identity"][token] = tokens[token]
            else:
                _LOGGER.warning(f"Something went wrong when refreshing {BRAND} account tokens.")
                return False

            body = {"grant_type": "id_token", "scope": "sc2:fal", "token": self._session_tokens["identity"]["id_token"]}

            response = await self._session.post(
                url="https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/token",
                headers=tHeaders,
                data=body,
                allow_redirects=True,
            )
            if response.status == 200:
                tokens = await response.json()
                if not await self.verify_tokens(tokens["access_token"], "vwg"):
                    _LOGGER.warning("Token could not be verified!")
                for token in tokens:
                    self._session_tokens["vwg"][token] = tokens[token]
            else:
                resp = await response.text()
                _LOGGER.warning("Something went wrong when refreshing API tokens. %s" % resp)
                return False
            return True
        except Exception as error:
            _LOGGER.warning(f"Could not refresh tokens: {error}")
            return False

    async def set_token(self, type):
        """Switch between tokens."""
        self._session_headers["Authorization"] = "Bearer " + self._session_tokens[type]["access_token"]
        return

    # Class helpers #
    @property
    def vehicles(self):
        """Return list of Vehicle objects."""
        return self._vehicles

    @property
    def logged_in(self):
        """
        Return cached logged in state.

        Not actually checking anything.
        """
        return self._session_logged_in

    def vehicle(self, vin):
        """Return vehicle object for given vin."""
        return next((vehicle for vehicle in self.vehicles if vehicle.unique_id.lower() == vin.lower()), None)

    def hash_spin(self, challenge, spin):
        """Convert SPIN and challenge to hash."""
        spinArray = bytearray.fromhex(spin)
        byteChallenge = bytearray.fromhex(challenge)
        spinArray.extend(byteChallenge)
        return hashlib.sha512(spinArray).hexdigest()

    @property
    async def validate_login(self):
        """Check that we have a valid access token."""
        try:
            if not await self.validate_tokens:
                return False

            return True
        except OSError as error:
            _LOGGER.warning("Could not validate login: %s", error)
            return False


async def main():
    """Run the program."""
    if "-v" in sys.argv:
        logging.basicConfig(level=logging.INFO)
    elif "-vv" in sys.argv:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.ERROR)

    async with ClientSession(headers={"Connection": "keep-alive"}) as session:
        connection = Connection(session, **read_config())
        if await connection.doLogin():
            if await connection.update():
                for vehicle in connection.vehicles:
                    print(f"Vehicle id: {vehicle}")
                    print("Supported sensors:")
                    for instrument in vehicle.dashboard().instruments:
                        print(f" - {instrument.name} (domain:{instrument.component}) - {instrument.str_state}")


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
