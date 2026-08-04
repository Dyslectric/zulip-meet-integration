from django.db import models
from django.db.models import CASCADE, F, Q
from django.db.models.functions import Lower

from zerver.models.users import UserProfile


class AbstractPushDeviceToken(models.Model):
    APNS = 1
    FCM = 2

    KINDS = (
        (APNS, "apns"),
        # The string value in the database is "gcm" for legacy reasons.
        # TODO: We should migrate it.
        (FCM, "gcm"),
    )

    kind = models.PositiveSmallIntegerField(choices=KINDS)

    # The token is a unique device-specific token that is
    # sent to us from each device:
    #   - APNS token if kind == APNS
    #   - FCM registration id if kind == FCM
    token = models.CharField(max_length=4096, db_index=True)

    # TODO: last_updated should be renamed date_created, since it is
    # no longer maintained as a last_updated value.
    last_updated = models.DateTimeField(auto_now=True)

    # [optional] Contains the app id of the device if it is an iOS device
    ios_app_id = models.TextField(null=True)

    class Meta:
        abstract = True


class PushDeviceToken(AbstractPushDeviceToken):
    # The user whose device this is
    user = models.ForeignKey(UserProfile, db_index=True, on_delete=CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "user_id",
                "kind",
                Lower(F("token")),
                name="zerver_pushdevicetoken_apns_user_kind_token",
                condition=Q(kind=AbstractPushDeviceToken.APNS),
            ),
            models.UniqueConstraint(
                "user_id",
                "kind",
                "token",
                name="zerver_pushdevicetoken_fcm_user_kind_token",
                condition=Q(kind=AbstractPushDeviceToken.FCM),
            ),
        ]


class WebPushSubscription(models.Model):
    """A browser Web Push subscription (RFC 8030 / RFC 8291) for a user.

    The web client creates one of these after registering a service worker
    and calling PushManager.subscribe(). A user may have many, one per
    browser/profile they've enabled notifications in.
    """

    user_profile = models.ForeignKey(UserProfile, db_index=True, on_delete=CASCADE)

    # The push service URL the browser handed us; we POST encrypted payloads
    # to it. Unique per user, so re-subscribing updates in place.
    endpoint = models.TextField()

    # Public key and auth secret from the PushSubscription, used to encrypt
    # message payloads so only this browser can read them (RFC 8291).
    p256dh = models.TextField()
    auth = models.TextField()

    date_created = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "user_profile",
                "endpoint",
                name="zerver_webpushsubscription_user_endpoint",
            ),
        ]
