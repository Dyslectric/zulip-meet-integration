import datetime
import hashlib
import json
import logging
import random
from abc import ABC, abstractmethod
from base64 import b64encode
from typing import Any, Literal
from urllib.parse import quote, urlencode, urljoin, urlsplit

import requests
from defusedxml import ElementTree
from django.conf import settings
from django.core.signing import Signer
from django.http import HttpRequest, HttpResponse
from django.middleware import csrf
from django.shortcuts import redirect, render
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.timezone import now as timezone_now
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from oauthlib.oauth2 import OAuth2Error
from pydantic import Json
from requests import Response
from requests_oauthlib import OAuth2Session
from typing_extensions import TypedDict, override

from zerver.actions.video_calls import do_set_video_call_provider_token
from zerver.decorator import zulip_login_required
from zerver.lib.cache import (
    cache_with_key,
    flush_zoom_server_access_token_cache,
    zoom_server_access_token_cache_key,
)
from django.core.signing import BadSignature

from zerver.lib.exceptions import ErrorCode, JsonableError
from zerver.lib.jitsi_token import (
    build_user_context,
    channel_scope,
    derive_room_name,
    direct_message_scope,
    jitsi_jwt_is_configured,
    mint_jitsi_token,
)
from zerver.lib.message import truncate_content
from zerver.lib.outgoing_http import OutgoingSession
from zerver.lib.partial import partial
from zerver.lib.pysa import mark_sanitized
from zerver.lib.response import json_success
from zerver.lib.streams import access_stream_by_id
from zerver.lib.subdomains import get_subdomain
from zerver.lib.typed_endpoint import typed_endpoint, typed_endpoint_without_parameters
from zerver.lib.url_encoding import append_url_query_string
from zerver.lib.user_groups import is_user_in_group
from zerver.lib.users import access_user_by_id
from zerver.lib.utils import assert_is_not_none
from zerver.models import NamedUserGroup, UserProfile
from zerver.models.realms import get_realm


class VideoCallSession(OutgoingSession):
    def __init__(self) -> None:
        super().__init__(role="video_calls", timeout=5)


class InvalidVideoCallProviderTokenError(JsonableError):
    code = ErrorCode.INVALID_VIDEO_CALL_PROVIDER_TOKEN

    def __init__(self, provider_name: str) -> None:
        super().__init__(
            _("Invalid {provider_name} access token").format(provider_name=provider_name)
        )


class CreateVideoCallFailedError(JsonableError):
    def __init__(self, provider_name: str) -> None:
        super().__init__(
            _("Failed to create {provider_name} call").format(provider_name=provider_name)
        )


class VideoCallProviderNotConfiguredError(JsonableError):
    def __init__(self, provider_name: str) -> None:
        super().__init__(
            _("{provider_name} credentials have not been configured").format(
                provider_name=provider_name
            )
        )


class VideoCallProviderCredentialsError(JsonableError):
    def __init__(self, provider_name: str) -> None:
        super().__init__(
            _("Invalid {provider_name} credentials").format(provider_name=provider_name)
        )


class VideoCallServerConnectionError(JsonableError):
    def __init__(self, provider_name: str, reason: str) -> None:
        super().__init__(
            _("Error connecting to the {provider_name} server: {reason}").format(
                provider_name=provider_name, reason=reason
            )
        )


class VideoCallServerAuthError(JsonableError):
    def __init__(self, provider_name: str) -> None:
        super().__init__(
            _("Error authenticating to the {provider_name} server").format(
                provider_name=provider_name
            )
        )


class VideoCallServerStatusError(JsonableError):
    def __init__(self, provider_name: str, status: str | None) -> None:
        super().__init__(
            _("{provider_name} server returned an unexpected error: {status}").format(
                provider_name=provider_name, status=status
            )
        )


class VideoCallProviderSessionIdError(JsonableError):
    def __init__(self, provider_name: str) -> None:
        super().__init__(
            _("Invalid {provider_name} session identifier").format(provider_name=provider_name)
        )


class UnknownZoomUserError(JsonableError):
    code = ErrorCode.UNKNOWN_ZOOM_USER

    def __init__(self) -> None:
        super().__init__(_("Unknown Zoom user email"))


class ConstructorGroupsService:
    def __init__(self) -> None:
        if (
            (url := settings.CONSTRUCTOR_GROUPS_URL) is None
            or (access_key := settings.CONSTRUCTOR_GROUPS_ACCESS_KEY) is None
            or (secret_key := settings.CONSTRUCTOR_GROUPS_SECRET_KEY) is None
        ):
            raise VideoCallProviderNotConfiguredError("Constructor Groups")

        self.access_key = access_key
        self.secret_key = secret_key
        self.base_url = url.rstrip("/")

    def _make_authenticated_request(
        self, method: str, endpoint: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make authenticated request to Constructor Groups XAPI"""
        url = f"{self.base_url}{endpoint}"

        # Construct authentication token: ACCESS_KEY|SHA256(ACCESS_KEY|SECRET_KEY)
        combined_string = f"{self.access_key}|{self.secret_key}"
        hash_hex = hashlib.sha256(combined_string.encode("utf-8")).hexdigest()
        auth_token = f"{self.access_key}|{hash_hex}"

        headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }

        try:
            session = VideoCallSession()
            if method.upper() == "POST":
                response = session.post(url, headers=headers, json=data or {})
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            response.raise_for_status()
            return response.json()
        except requests.HTTPError as e:
            logging.exception(
                "Constructor Groups API request failed with status %s", e.response.status_code
            )
            raise CreateVideoCallFailedError("Constructor Groups")
        except Exception:
            logging.exception("Constructor Groups API request failed")
            raise CreateVideoCallFailedError("Constructor Groups")

    def get_or_create_default_room(
        self, creator_email: str, name: str, fallback_name: str
    ) -> dict[str, Any]:
        data = {
            "creator_email": creator_email,
            "name": name,
            "fallback_name": fallback_name,
        }

        return self._make_authenticated_request("POST", "/room/default", data)


class OAuthVideoCallProvider(ABC):
    provider_name: str = NotImplemented
    client_id: str | None = NotImplemented
    client_secret: str | None = NotImplemented
    authorization_scope: str | None = NotImplemented
    authorization_url: str = NotImplemented
    token_url: str = NotImplemented
    auto_refresh_url: str = NotImplemented
    create_meeting_url: str = NotImplemented
    token_key_name: str = NotImplemented

    def get_token(self, user: UserProfile) -> object | None:
        return user.third_party_api_state.get(self.token_key_name)

    def update_token(self, user: UserProfile, token: dict[str, object] | None) -> None:
        do_set_video_call_provider_token(user, self.token_key_name, token)

    @abstractmethod
    def get_meeting_details(self, request: HttpRequest, response: Response) -> HttpResponse:
        pass

    def __get_session(self, user: UserProfile) -> OAuth2Session:
        if self.client_id is None or self.client_secret is None:
            raise VideoCallProviderNotConfiguredError(self.provider_name)

        return OAuth2Session(
            self.client_id,
            scope=self.authorization_scope,
            redirect_uri=urljoin(
                settings.ROOT_DOMAIN_URI, f"/calls/{self.provider_name.lower()}/complete"
            ),
            auto_refresh_url=self.auto_refresh_url,
            auto_refresh_kwargs={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            token=self.get_token(user),
            token_updater=partial(self.update_token, user),
        )

    def __get_sid(self, request: HttpRequest) -> str:
        # This is used to prevent CSRF attacks on the OAuth
        # authentication flow.  We want this value to be unpredictable and
        # tied to the session, but we don’t want to expose the main CSRF
        # token directly to the server.

        csrf.get_token(request)
        # Use 'mark_sanitized' to cause Pysa to ignore the flow of user controlled
        # data out of this function. 'request.META' is indeed user controlled, but
        # post-HMAC output is no longer meaningfully controllable.
        return mark_sanitized(
            ""
            if getattr(request, "_dont_enforce_csrf_checks", False)
            else salted_hmac(
                f"Zulip {self.provider_name.capitalize()} sid", request.META["CSRF_COOKIE"]
            ).hexdigest()
        )

    def register_user(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        assert isinstance(request.user, UserProfile)
        oauth = self.__get_session(request.user)
        authorization_url, _state = oauth.authorization_url(
            self.authorization_url,
            state=json.dumps(
                {"realm": get_subdomain(request), "sid": self.__get_sid(request)},
            ),
            **kwargs,
        )
        return redirect(authorization_url)

    def complete_user(
        self, request: HttpRequest, sid: str, code: str, **kwargs: Any
    ) -> HttpResponse:
        if not constant_time_compare(sid, self.__get_sid(request)):
            raise VideoCallProviderSessionIdError(self.provider_name)
        assert isinstance(request.user, UserProfile)
        oauth = self.__get_session(request.user)
        try:
            token = oauth.fetch_token(
                self.token_url, code=code, client_secret=self.client_secret, **kwargs
            )
        except OAuth2Error:
            raise VideoCallProviderCredentialsError(self.provider_name)

        self.update_token(request.user, token)
        return render(request, "zerver/close_window.html")

    def oauth_post(
        self,
        user: UserProfile,
        url: str,
        *,
        json: object = None,
        **kwargs: Any,
    ) -> Response:
        oauth = self.__get_session(user)
        if not oauth.authorized:
            raise InvalidVideoCallProviderTokenError(self.provider_name)

        try:
            response = oauth.post(url, json=json, **kwargs)
        except OAuth2Error:
            self.update_token(user, None)
            raise InvalidVideoCallProviderTokenError(self.provider_name)

        if response.status_code == 401:
            self.update_token(user, None)
            raise InvalidVideoCallProviderTokenError(self.provider_name)

        return response

    def make_video_call(
        self, request: HttpRequest, user: UserProfile, payload: object = {}, **kwargs: Any
    ) -> HttpResponse:
        response = self.oauth_post(user, self.create_meeting_url, json=payload, **kwargs)
        if not response.ok:
            raise CreateVideoCallFailedError(self.provider_name)

        return self.get_meeting_details(request, response)


class WebexOAuthProvider(OAuthVideoCallProvider):
    provider_name = "Webex"
    token_key_name = "webex"

    def __init__(self) -> None:
        self.client_id = settings.VIDEO_WEBEX_CLIENT_ID
        self.client_secret = settings.VIDEO_WEBEX_CLIENT_SECRET
        self.authorization_url = urljoin(settings.VIDEO_WEBEX_API_URL, "authorize")
        self.token_url = urljoin(settings.VIDEO_WEBEX_API_URL, "access_token")
        self.auto_refresh_url = urljoin(settings.VIDEO_WEBEX_API_URL, "access_token")
        self.create_meeting_url = urljoin(settings.VIDEO_WEBEX_API_URL, "meetings")
        self.create_room_url = urljoin(settings.VIDEO_WEBEX_API_URL, "rooms")
        # We need spark:all to create meetings with an associated roomId, which are
        # ad-hoc meetings in our case: https://developer.webex.com/meeting/docs/meetings.
        self.authorization_scope = "meeting:schedules_read meeting:schedules_write spark:all"

    @override
    def get_meeting_details(self, request: HttpRequest, response: Response) -> HttpResponse:
        return json_success(request, data={"url": response.json()["webLink"]})

    # For ad-hoc meetings, we need to create a public room as a prerequisite,
    # where a public room is a room that lets anyone in the Webex organization
    # join meetings associated with it. Note that the public room feature is
    # restricted to paid Webex organizations.
    def maybe_generate_public_room_id(self, user: UserProfile) -> str | None:
        create_room_payload = {
            "title": "Webex Zulip Meeting",
            "isPublic": True,
            "description": "A Webex meeting created via the Zulip client.",
        }
        response = self.oauth_post(user, self.create_room_url, json=create_room_payload)

        # This is probably a free organization, because the Webex API sends a 403
        # response when trying to create a public room with user credentials that
        # belong to a free organization with the message: "Spaces belonging to a
        # free org cannot be made public". For details, see error docs at
        # https://developer.webex.com/messaging/docs/api/v1/rooms/create-a-room.
        if response.status_code == 403:
            return None
        elif not response.ok:
            raise JsonableError(
                _("Failed to create {provider_name} public room.").format(
                    provider_name=self.provider_name
                )
            )
        return response.json()["id"]


class ZoomGeneralOAuthProvider(OAuthVideoCallProvider):
    provider_name = "Zoom"
    authorization_scope = None
    token_key_name = "zoom"

    def __init__(self) -> None:
        self.client_id = settings.VIDEO_ZOOM_CLIENT_ID
        self.client_secret = settings.VIDEO_ZOOM_CLIENT_SECRET
        self.authorization_url = urljoin(settings.VIDEO_ZOOM_OAUTH_URL, "/oauth/authorize")
        self.token_url = urljoin(settings.VIDEO_ZOOM_OAUTH_URL, "/oauth/token")
        self.auto_refresh_url = urljoin(settings.VIDEO_ZOOM_OAUTH_URL, "/oauth/token")
        self.create_meeting_url = urljoin(settings.VIDEO_ZOOM_API_URL, "/v2/users/me/meetings")

    @override
    def get_meeting_details(self, request: HttpRequest, response: Response) -> HttpResponse:
        return json_success(request, data={"url": response.json()["join_url"]})


@zulip_login_required
@never_cache
def register_zoom_user(request: HttpRequest) -> HttpResponse:
    return ZoomGeneralOAuthProvider().register_user(request=request)


@zulip_login_required
@never_cache
def register_webex_user(request: HttpRequest) -> HttpResponse:
    return WebexOAuthProvider().register_user(request=request)


class StateDictRealm(TypedDict):
    realm: str
    sid: str


class StateDict(TypedDict):
    sid: str


class ZoomVideoSettings(TypedDict):
    host_video: bool
    participant_video: bool


class ZoomPayload(TypedDict):
    settings: ZoomVideoSettings
    default_password: bool


class WebexPersonalRoomMeetingPayload(TypedDict):
    title: str
    start: str
    end: str
    scheduledType: Literal["personalRoomMeeting"]


class WebexAdhocMeetingPayload(TypedDict):
    title: str
    adhoc: Literal[True]
    roomId: str


@never_cache
@zulip_login_required
@typed_endpoint
def complete_zoom_user(
    request: HttpRequest,
    *,
    code: str,
    state: Json[StateDictRealm],
) -> HttpResponse:
    if get_subdomain(request) != state["realm"]:
        return redirect(urljoin(get_realm(state["realm"]).url, request.get_full_path()))
    return ZoomGeneralOAuthProvider().complete_user(request, code=code, sid=state["sid"])


@never_cache
@zulip_login_required
@typed_endpoint
def complete_webex_user(
    request: HttpRequest,
    *,
    code: str,
    state: Json[StateDictRealm],
) -> HttpResponse:
    if get_subdomain(request) != state["realm"]:
        return redirect(urljoin(get_realm(state["realm"]).url, request.get_full_path()))
    return WebexOAuthProvider().complete_user(request, code=code, sid=state["sid"])


@cache_with_key(zoom_server_access_token_cache_key, timeout=3600 - 240)
def get_zoom_server_to_server_access_token(account_id: str) -> str:
    if settings.VIDEO_ZOOM_CLIENT_ID is None:
        raise VideoCallProviderNotConfiguredError("Zoom")

    client_id = settings.VIDEO_ZOOM_CLIENT_ID.encode("utf-8")
    client_secret = str(settings.VIDEO_ZOOM_CLIENT_SECRET).encode("utf-8")

    url = urljoin(settings.VIDEO_ZOOM_OAUTH_URL, "/oauth/token")
    data = {"grant_type": "account_credentials", "account_id": account_id}

    client_information = client_id + b":" + client_secret
    encoded_client = b64encode(client_information).decode("ascii")
    headers = {"Host": urlsplit(url).hostname, "Authorization": f"Basic {encoded_client}"}

    response = VideoCallSession().post(url, data, headers=headers)
    if not response.ok:
        # {reason: 'Bad request', error: 'invalid_request'} for invalid account ID
        # {'reason': 'Invalid client_id or client_secret', 'error': 'invalid_client'}
        raise VideoCallProviderCredentialsError("Zoom")
    return response.json()["access_token"]


def get_zoom_server_to_server_call(
    user: UserProfile, access_token: str, payload: ZoomPayload
) -> str:
    email = user.delivery_email
    url = f"{settings.VIDEO_ZOOM_API_URL}/v2/users/{email}/meetings"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    response = VideoCallSession().post(url, json=payload, headers=headers)
    if not response.ok:
        response_dict = response.json()
        zoom_api_error_code = response_dict["code"]
        if zoom_api_error_code == 1001:
            # {code: 1001, message: "User does not exist: {email}"}
            raise UnknownZoomUserError
        if zoom_api_error_code == 124:
            # For the error responses below, we flush any
            # cached access token for the Zoom account.
            # {code: 124, message: "Invalid access token"}
            # {code: 124, message: "Access token is expired"}
            account_id = str(settings.VIDEO_ZOOM_SERVER_TO_SERVER_ACCOUNT_ID)

            # We are managing expiry ourselves, so this shouldn't
            # happen. Log an error, and flush the access token from
            # the cache, so that future requests should proceed.
            logging.error(
                "Unexpected Zoom error 124: %s",
                response_dict.get("message", str(response_dict)),
            )
            flush_zoom_server_access_token_cache(account_id)
        raise CreateVideoCallFailedError("Zoom")
    return response.json()["join_url"]


def make_server_authenticated_zoom_video_call(
    request: HttpRequest,
    user: UserProfile,
    *,
    payload: ZoomPayload,
) -> HttpResponse:
    account_id = str(settings.VIDEO_ZOOM_SERVER_TO_SERVER_ACCOUNT_ID)
    access_token = get_zoom_server_to_server_access_token(account_id)
    url = get_zoom_server_to_server_call(user, access_token, payload)
    return json_success(request, data={"url": url})


@typed_endpoint
def make_zoom_video_call(
    request: HttpRequest,
    user: UserProfile,
    *,
    is_video_call: Json[bool] = True,
) -> HttpResponse:
    # The meeting host has the ability to configure both their own and
    # participants' default video on/off state for the meeting. That's
    # why when creating a meeting, configure the video on/off default
    # according to the desired call type. Each Zoom user can still have
    # their own personal setting to not start video by default.
    video_settings = ZoomVideoSettings(host_video=is_video_call, participant_video=is_video_call)
    payload = ZoomPayload(
        settings=video_settings,
        # Generate a default password depending on the user settings. This will
        # result in the password being appended to the returned Join URL.
        #
        # If we don't request a password to be set, the waiting room will be
        # forcibly enabled in Zoom organizations that require some kind of
        # authentication for all meetings.
        default_password=True,
    )
    if settings.VIDEO_ZOOM_SERVER_TO_SERVER_ACCOUNT_ID is not None:
        return make_server_authenticated_zoom_video_call(request, user, payload=payload)
    return ZoomGeneralOAuthProvider().make_video_call(request=request, user=user, payload=payload)


@typed_endpoint_without_parameters
def make_webex_video_call(request: HttpRequest, user: UserProfile) -> HttpResponse:
    room_id = WebexOAuthProvider().maybe_generate_public_room_id(user)
    payload: WebexAdhocMeetingPayload | WebexPersonalRoomMeetingPayload

    # Quoting from https://developer.webex.com/meeting/docs/api/v1/meetings/create-a-meeting:
    # An ad-hoc meeting is a non-recurring instant meeting for the target room
    # which is supposed to be started immediately after being created for a
    # quick collaboration.

    # Public room IDs can only be generated if the user belongs to a paid Webex
    # organization. They are necessary to generate ad-hoc Webex meetings which
    # can be joined by anyone in the Webex organization. We fall back to
    # generating a personal room meeting link if the user belongs to a free
    # Webex organization.
    if room_id is not None:
        payload = WebexAdhocMeetingPayload(adhoc=True, roomId=room_id, title="Webex Ad-hoc meeting")
    else:
        start_time = timezone_now()
        end_time = start_time + datetime.timedelta(minutes=40)
        payload = WebexPersonalRoomMeetingPayload(
            scheduledType="personalRoomMeeting",
            start=start_time.isoformat(timespec="seconds"),
            end=end_time.isoformat(timespec="seconds"),
            title="Webex Personal Room Meeting",
        )

    return WebexOAuthProvider().make_video_call(request, user, payload=payload)


@csrf_exempt
@require_POST
@typed_endpoint_without_parameters
def deauthorize_zoom_user(request: HttpRequest) -> HttpResponse:
    return json_success(request)


@typed_endpoint
def get_bigbluebutton_url(
    request: HttpRequest,
    user_profile: UserProfile,
    *,
    meeting_name: str,
    voice_only: Json[bool] = False,
) -> HttpResponse:
    # https://docs.bigbluebutton.org/dev/api.html#create for reference on the API calls
    # https://docs.bigbluebutton.org/dev/api.html#usage for reference for checksum
    id = "zulip-" + str(random.randint(100000000000, 999999999999))

    # We sign our data here to ensure a Zulip user cannot tamper with
    # the join link to gain access to other meetings that are on the
    # same bigbluebutton server.
    signed = Signer().sign_object(
        {
            "meeting_id": id,
            "name": meeting_name,
            "lock_settings_disable_cam": voice_only,
            "moderator": request.user.id,
        }
    )
    url = append_url_query_string("/calls/bigbluebutton/join", "bigbluebutton=" + signed)
    return json_success(request, {"url": url})


# We use zulip_login_required here mainly to get access to the user's
# full name from Zulip to prepopulate the user's name in the
# BigBlueButton meeting.  Since the meeting's details are encoded in
# the link the user is clicking, there is no validation tying this
# meeting to the Zulip organization it was created in.
@zulip_login_required
@never_cache
@typed_endpoint
def join_bigbluebutton(request: HttpRequest, *, bigbluebutton: str) -> HttpResponse:
    assert request.user.is_authenticated

    if settings.BIG_BLUE_BUTTON_URL is None or settings.BIG_BLUE_BUTTON_SECRET is None:
        raise VideoCallProviderNotConfiguredError("BigBlueButton")

    try:
        bigbluebutton_data = Signer().unsign_object(bigbluebutton)
    except Exception:
        raise JsonableError(_("Invalid signature."))

    create_params = urlencode(
        {
            "meetingID": bigbluebutton_data["meeting_id"],
            "name": bigbluebutton_data["name"],
            "lockSettingsDisableCam": bigbluebutton_data["lock_settings_disable_cam"],
        },
        quote_via=quote,
    )

    checksum = hashlib.sha256(
        ("create" + create_params + settings.BIG_BLUE_BUTTON_SECRET).encode()
    ).hexdigest()

    try:
        response = VideoCallSession().get(
            append_url_query_string(settings.BIG_BLUE_BUTTON_URL + "api/create", create_params)
            + "&checksum="
            + checksum
        )
        response.raise_for_status()
    except requests.RequestException as e:
        if e.response is not None:
            reason = f"HTTP {response.status_code}: {response.text:.200}"
        else:
            reason = str(e)
        raise VideoCallServerConnectionError("BigBlueButton", reason=reason)

    payload = ElementTree.fromstring(response.text)
    if assert_is_not_none(payload.find("messageKey")).text == "checksumError":
        raise VideoCallServerAuthError("BigBlueButton")

    status = assert_is_not_none(payload.find("returncode")).text
    if status != "SUCCESS":
        raise VideoCallServerStatusError("BigBlueButton", status=status)

    join_params = urlencode(
        {
            "meetingID": bigbluebutton_data["meeting_id"],
            # We use the moderator role only for the user who created the
            # meeting, the attendee role for everyone else, so that only
            # the user who created the meeting can convert a voice-only
            # call to a video call.
            "role": "MODERATOR" if bigbluebutton_data["moderator"] == request.user.id else "VIEWER",
            "fullName": request.user.full_name,
            # https://docs.bigbluebutton.org/dev/api.html#create
            # The createTime option is used to have the user redirected to a link
            # that is only valid for this meeting.
            #
            # Even if the same link in Zulip is used again, a new
            # createTime parameter will be created, as the meeting on
            # the BigBlueButton server has to be recreated. (after a
            # few minutes)
            "createTime": assert_is_not_none(payload.find("createTime")).text,
        },
        quote_via=quote,
    )

    checksum = hashlib.sha256(
        ("join" + join_params + settings.BIG_BLUE_BUTTON_SECRET).encode()
    ).hexdigest()
    redirect_url_base = append_url_query_string(
        settings.BIG_BLUE_BUTTON_URL + "api/join", join_params
    )
    return redirect(append_url_query_string(redirect_url_base, "checksum=" + checksum))


@typed_endpoint_without_parameters
def make_constructor_groups_video_call(
    request: HttpRequest,
    user_profile: UserProfile,
) -> HttpResponse:
    service = ConstructorGroupsService()
    room_name = _("{full_name}'s Zulip room").format(full_name=user_profile.full_name)

    room_data = service.get_or_create_default_room(
        creator_email=user_profile.delivery_email,
        name=room_name,
        fallback_name=f"{room_name} ({user_profile.realm_id}-{user_profile.id})",
    )

    room_url = room_data.get("url", "")
    if not room_url:
        logging.error("Constructor Groups API returned room without URL: %s", room_data)
        raise CreateVideoCallFailedError("Constructor Groups")

    return json_success(request, {"url": room_url})


# Nextcloud Talk API limits room names to 255 characters.
MAX_NEXTCLOUD_TALK_ROOM_NAME_LENGTH = 255


@typed_endpoint
def create_nextcloud_talk_url(
    request: HttpRequest, user: UserProfile, *, room_name: str
) -> HttpResponse:
    if (
        settings.NEXTCLOUD_SERVER is None
        or settings.NEXTCLOUD_TALK_USERNAME is None
        or settings.NEXTCLOUD_TALK_PASSWORD is None
    ):
        raise VideoCallProviderNotConfiguredError("Nextcloud Talk")

    room_name = truncate_content(room_name, MAX_NEXTCLOUD_TALK_ROOM_NAME_LENGTH, "...")
    # https://nextcloud-talk.readthedocs.io/en/stable/conversation/#creating-a-new-conversation
    api_url = urljoin(settings.NEXTCLOUD_SERVER, "/ocs/v2.php/apps/spreed/api/v4/room")

    payload = {
        # Create a PUBLIC conversation (roomType=3) which allows guest access
        # https://nextcloud-talk.readthedocs.io/en/latest/constants/#conversation-types
        "roomType": 3,
        "roomName": room_name,
    }
    username = str(settings.NEXTCLOUD_TALK_USERNAME)
    password = str(settings.NEXTCLOUD_TALK_PASSWORD)
    credentials = f"{username}:{password}".encode()
    encoded_credentials = b64encode(credentials).decode("ascii")

    headers = {
        "OCS-APIRequest": "true",
        "Accept": "application/json",
        "Authorization": f"Basic {encoded_credentials}",
    }

    try:
        response = VideoCallSession().post(api_url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        if e.response is not None:
            reason = f"HTTP {response.status_code}: {response.text:.200}"
        else:
            reason = str(e)
        raise VideoCallServerConnectionError("Nextcloud Talk", reason=reason)
    try:
        data = response.json()
        token = data["ocs"]["data"]["token"]
    except (KeyError, ValueError):
        raise CreateVideoCallFailedError("Nextcloud Talk")

    call_url = urljoin(settings.NEXTCLOUD_SERVER, f"/index.php/call/{token}")
    return json_success(request, data={"url": call_url})


# -- Jitsi Meet with JWT ------------------------------------------------------
#
# Unlike the other providers here, this one performs an authorization check. The
# existing endpoints let any realm member mint a call for any room name, which is
# harmless when the resulting room is unauthenticated anyway. Once Prosody starts
# trusting our signature, it stops being harmless: the token is the only thing
# standing between a user and a conversation they are not part of.


def resolve_jitsi_tenant(user: UserProfile) -> str:
    """Map a user to a Jitsi tenant.

    Tenant-style URLs are what make isolation structural rather than a matter of
    our own good intentions: Prosody's domain verification refuses a token whose
    `sub` does not match the tenant in the URL, so a token minted for one tenant
    cannot open a room in another even if this code is wrong about which room.
    """
    if settings.JITSI_TENANT_BY_GROUP:
        # Sorted for determinism: a user in two mapped groups must always get
        # the same tenant, or their room name changes between calls.
        for group_name in sorted(settings.JITSI_TENANT_BY_GROUP):
            try:
                group = NamedUserGroup.objects.get(
                    name=group_name, realm=user.realm, is_system_group=False
                )
            except NamedUserGroup.DoesNotExist:
                continue
            if is_user_in_group(group.id, user):
                return settings.JITSI_TENANT_BY_GROUP[group_name].lower()

    if settings.JITSI_DEFAULT_TENANT is not None:
        return settings.JITSI_DEFAULT_TENANT.lower()
    return user.realm.subdomain.lower()


EPOCH_SIGNER_SALT = "zerver.views.video_calls.jitsi_epoch"


def sign_jitsi_epoch(scope: str, epoch: int) -> str:
    return Signer(salt=EPOCH_SIGNER_SALT).sign_object({"scope": scope, "epoch": epoch})


def unsign_jitsi_epoch(scope: str, epoch_token: str | None) -> int:
    """Recover the room epoch from a token we previously issued.

    The epoch rotates a conversation's room. It is signed and round-tripped
    rather than stored so that this endpoint stays stateless, and bound to its
    scope so that an epoch issued for one conversation cannot be replayed
    against another to force a room that a later caller would also derive.
    """
    if epoch_token is None:
        return 0
    try:
        data = Signer(salt=EPOCH_SIGNER_SALT).unsign_object(epoch_token)
    except BadSignature:
        raise JsonableError(_("Invalid epoch token"))
    if not isinstance(data, dict) or data.get("scope") != scope:
        raise JsonableError(_("Invalid epoch token"))
    epoch = data.get("epoch")
    if not isinstance(epoch, int) or epoch < 0:
        raise JsonableError(_("Invalid epoch token"))
    return epoch

logger = logging.getLogger(__name__)


def notify_conferencing_service(user, *, scope, room, tenant, stream_id, user_ids=None):
    """Best-effort: tell the conferencing service a call was minted.

    The service owns the call message and occupancy roster but can't reverse a
    room name (a one-way HMAC) back to a conversation — the mint is the only
    place that knows. Errors are swallowed so a down service never breaks a call.
    Channel and DM/group calls are both notified now: under the core-hook design
    the service posts a DM/group message authored as the initiator (see
    zerver/views/jitsi_hook.py), so it lands in the real conversation. For a DM we
    send the full participant set (initiator included) for the service to post.
    """
    url = getattr(settings, "JITSI_CONFERENCING_URL", None)
    if not url:
        return
    participants = None
    if stream_id is None:
        participants = sorted(set(user_ids or []) | {user.id})
    try:
        requests.post(
            url.rstrip("/") + "/api/v1/jitsi/calls/created",
            json={
                "room": room, "tenant": tenant, "scope": scope,
                "realm_id": user.realm_id, "realm_subdomain": user.realm.subdomain,
                "stream_id": stream_id, "user_ids": participants,
                "initiator_id": user.id,
                "initiator_name": user.full_name,
                "topic": getattr(settings, "JITSI_CALL_TOPIC", "Calls"),
            },
            headers={"Authorization": f"Bearer {getattr(settings, 'JITSI_CONFERENCING_SECRET', '')}"},
            timeout=2,
            proxies={"http": None, "https": None},
        )
    except Exception:
        logger.warning("could not notify conferencing service of call in %s", room, exc_info=True)

@typed_endpoint
def create_jitsi_call(
    request: HttpRequest,
    user: UserProfile,
    *,
    stream_id: Json[int] | None = None,
    user_ids: Json[list[int]] | None = None,
    epoch_token: str | None = None,
    rotate: Json[bool] = False,
) -> HttpResponse:
    if settings.JITSI_SERVER_URL is None:
        raise VideoCallProviderNotConfiguredError("Jitsi Meet")
    if not jitsi_jwt_is_configured():
        raise VideoCallProviderNotConfiguredError("Jitsi Meet (JWT)")

    if (stream_id is None) == (user_ids is None):
        raise JsonableError(_("Specify exactly one of stream_id or user_ids"))

    is_moderator = user.is_realm_admin
    if stream_id is not None:
        # access_stream_by_id raises unless the user can reach the channel at
        # all; `sub` is None when they can read it without being subscribed, and
        # subscription is what we treat as membership.
        stream, sub = access_stream_by_id(user, stream_id)
        if sub is None:
            raise JsonableError(_("Not subscribed to this channel"))
        if not stream.voice_video_enabled or stream.is_web_public:
            # Enforced here, not just hidden in the UI: without this a client
            # could still ask for a token for a channel with calls turned off. A
            # web-public channel never gets one whatever its setting says: a call
            # is for a known set of people, and anyone on the internet can read
            # such a channel.
            raise JsonableError(_("Voice and video calls are disabled in this channel"))
        scope = channel_scope(user.realm_id, stream.id)
        # Moderator maps to "may administer this channel", not to "whoever
        # clicked first", which is what default Jitsi would otherwise do.
        is_moderator = is_moderator or is_user_in_group(
            stream.can_administer_channel_group_id, user
        )
    else:
        assert user_ids is not None
        # Validate every recipient is a real, reachable account in this realm,
        # so a caller cannot derive a room for a conversation that could not
        # exist.
        for user_id in set(user_ids) - {user.id}:
            access_user_by_id(user, user_id, allow_bots=True, for_admin=False)
        scope = direct_message_scope(user.realm_id, [*user_ids, user.id])

    epoch = unsign_jitsi_epoch(scope, epoch_token)
    if rotate:
        epoch += 1

    room = derive_room_name(scope, epoch)
    tenant = resolve_jitsi_tenant(user)

    token = mint_jitsi_token(
        tenant=tenant,
        room=room,
        group=tenant,
        user_context=build_user_context(
            user_id=user.id,
            full_name=user.full_name,
            email=user.delivery_email if user.email_address_is_realm_public() else "",
            is_moderator=is_moderator,
        ),
    )

    base_url = user.realm.jitsi_server_url or settings.JITSI_SERVER_URL
    url = f"{base_url.rstrip('/')}/{tenant}/{room}"

    notify_conferencing_service(
        user, scope=scope, room=room, tenant=tenant, stream_id=stream_id, user_ids=user_ids
    )
    return json_success(
        request,
        {
            "url": append_url_query_string(url, urlencode({"jwt": token})),
            "room": room,
            "tenant": tenant,
            "epoch_token": sign_jitsi_epoch(scope, epoch),
        },
    )

@typed_endpoint
def get_jitsi_occupancy(
    request: HttpRequest,
    user: UserProfile,
    *,
    stream_id: Json[int],
) -> HttpResponse:
    """Occupancy of a channel's live call, for the presence widget.

    Runs as the user, so it enforces the same channel access the call endpoint
    does before revealing who is in a call — `access_stream_by_id` raises unless
    the user can reach the channel. The conferencing service holds the occupancy;
    this proxies to it and never trusts the browser with the service's address or
    secret. `proxies={}` bypasses the SSRF proxy (smokescreen) for this trusted
    internal target, the same reason `notify` does.

    Best-effort: if the service is unreachable, report an empty, inactive call
    rather than erroring — a missing widget is better than a broken compose box.
    """
    access_stream_by_id(user, stream_id)  # entitlement: raises if no access

    empty = {"stream_id": stream_id, "active": False, "count": 0, "occupants": [], "drifted": False}
    url = getattr(settings, "JITSI_CONFERENCING_URL", None)
    if not url:
        return json_success(request, empty)
    try:
        response = requests.get(
            url.rstrip("/") + "/api/v1/jitsi/occupancy",
            params={"stream_id": stream_id},
            headers={
                "Authorization": f"Bearer {getattr(settings, 'JITSI_CONFERENCING_SECRET', '')}"
            },
            proxies={"http": None, "https": None},
            timeout=2,
        )
        data = response.json()
    except Exception:
        logger.warning("could not fetch occupancy for channel %s", stream_id, exc_info=True)
        return json_success(request, empty)
    return json_success(request, data)


@typed_endpoint_without_parameters
def get_jitsi_occupancy_all(
    request: HttpRequest,
    user: UserProfile,
) -> HttpResponse:
    """Occupancy of every live channel call this user can see, for the sidebar.

    The sidebar shows call state across all of a user's channels at once, so it
    asks for them in one request rather than polling each. The conferencing
    service returns every active channel call in the deployment; this filters that
    down to the channels this user may actually reach — `access_stream_by_id`
    raises for one they cannot, and that room is dropped — so the response never
    reveals a call in a channel the user has no access to. Best-effort like
    `get_jitsi_occupancy`: an unreachable service yields an empty list, not an error.
    """
    empty: dict[str, Any] = {"rooms": []}
    url = getattr(settings, "JITSI_CONFERENCING_URL", None)
    if not url:
        return json_success(request, empty)
    try:
        response = requests.get(
            url.rstrip("/") + "/api/v1/jitsi/occupancy_all",
            headers={
                "Authorization": f"Bearer {getattr(settings, 'JITSI_CONFERENCING_SECRET', '')}"
            },
            proxies={"http": None, "https": None},
            timeout=2,
        )
        data = response.json()
    except Exception:
        logger.warning("could not fetch bulk jitsi occupancy", exc_info=True)
        return json_success(request, empty)

    rooms = data.get("rooms", []) if isinstance(data, dict) else []
    visible: list[dict[str, Any]] = []
    for room in rooms:
        stream_id = room.get("stream_id")
        if isinstance(stream_id, int):
            try:
                access_stream_by_id(user, stream_id)
            except JsonableError:
                continue  # user can't reach this channel: drop its call from the feed
            visible.append(room)
            continue

        # A DM/group call. Being one of its participants is the entitlement, so
        # a user never learns about a call in a conversation they are not in.
        user_ids = room.get("user_ids")
        if not isinstance(user_ids, list) or user.id not in user_ids:
            continue
        realm_id = room.get("realm_id")
        if realm_id is not None and realm_id != user.realm_id:
            continue
        visible.append(room)
    return json_success(request, {"rooms": visible})

