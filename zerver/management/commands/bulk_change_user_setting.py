from argparse import ArgumentParser
from typing import Any

from django.core.management.base import CommandError
from typing_extensions import override

from zerver.actions.realm_settings import do_set_realm_user_default_setting
from zerver.actions.user_settings import bulk_change_user_setting
from zerver.lib.management import ZulipBaseCommand
from zerver.models import RealmUserDefault, UserProfile


class Command(ZulipBaseCommand):
    help = """Set a user setting for a whole realm.

Sets the organization's default, which is what new accounts are created with.
Existing accounts keep whatever they already had, since the default is only
consulted at account creation -- pass --existing-users to change them too.

Example, to stop notifying people who already have Zulip open and active:

  manage.py bulk_change_user_setting -r yourrealm \\
      enable_online_push_notifications False --existing-users
"""

    @override
    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "setting_name",
            metavar="<setting_name>",
            help="name of the setting, as it appears in the API (e.g. enable_online_push_notifications)",
        )
        parser.add_argument(
            "value",
            metavar="<value>",
            help="new value; true/false for a boolean setting",
        )
        parser.add_argument(
            "--existing-users",
            action="store_true",
            help="also apply the value to every active non-bot account that already exists",
        )
        self.add_realm_args(parser, required=True)

    def parse_value(self, setting_name: str, raw_value: str) -> bool | int | str:
        # RealmUserDefault and UserProfile both inherit property_types from
        # UserBaseSettings, so one lookup covers the setting on both.
        property_types = RealmUserDefault.property_types
        if setting_name not in property_types:
            raise CommandError(
                f"'{setting_name}' is not a user setting. "
                f"Choose from: {', '.join(sorted(property_types))}"
            )

        setting_type = property_types[setting_name]
        if setting_type is bool:
            if raw_value.lower() in ("true", "t", "yes", "y", "1"):
                return True
            if raw_value.lower() in ("false", "f", "no", "n", "0"):
                return False
            raise CommandError(f"'{raw_value}' is not a boolean; use True or False.")
        if setting_type is int:
            try:
                return int(raw_value)
            except ValueError:
                raise CommandError(f"'{raw_value}' is not an integer.")
        if setting_type is str:
            return raw_value

        # Enum-valued settings would need their own mapping from the name a
        # human would type to the member. Refuse rather than guess.
        raise CommandError(
            f"'{setting_name}' has type {setting_type}, which this command cannot set."
        )

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        realm = self.get_realm(options)
        assert realm is not None

        setting_name = options["setting_name"]
        value = self.parse_value(setting_name, options["value"])

        realm_user_default = RealmUserDefault.objects.get(realm=realm)
        do_set_realm_user_default_setting(
            realm_user_default, setting_name, value, acting_user=None
        )
        print(f"Set the {realm.string_id} default for {setting_name} to {value}.")

        if not options["existing_users"]:
            print("Existing accounts were left alone; pass --existing-users to change them.")
            return

        # Bots have no sessions and no devices, so a notification setting means
        # nothing to them; deactivated accounts would be changed for no one.
        users = list(UserProfile.objects.filter(realm=realm, is_active=True, is_bot=False))
        if not users:
            print("No active accounts to change.")
            return

        # One event per user goes out to their open sessions, so a client that
        # is watching this setting updates without a reload.
        bulk_change_user_setting(realm, users, setting_name, value, acting_user=None)
        print(f"Set {setting_name} to {value} for {len(users)} existing account(s).")
