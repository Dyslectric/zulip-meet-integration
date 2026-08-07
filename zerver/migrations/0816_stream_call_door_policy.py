"""Replace Stream.require_moderator_to_join with the three-way call_door_policy.

The boolean could only say "a moderator must be present" or "nobody need be",
which turned out to be two of three useful answers. The third — that somebody
with a Zulip account must be present — is the one that matters on a web-public
channel, where it lets visitors join a conversation members are having without
letting them hold one among themselves.

Written as add-backfill-drop in one migration rather than by editing the
migration that introduced the boolean. That one has already been applied to
development databases, and rewriting an applied migration means every such
database is quietly wrong until somebody notices.
"""

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

# Spelled out rather than imported: a migration has to keep meaning what it meant
# when it ran, and an enum that later gains or reorders members would silently
# change history.
ANARCHY = 1
MODERATOR = 2


def set_door_policy_from_boolean(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Stream = apps.get_model("zerver", "Stream")
    Stream.objects.filter(require_moderator_to_join=True).update(call_door_policy=MODERATOR)


def set_boolean_from_door_policy(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Reverse. The third policy has no boolean to go back to, so it degrades to
    the strictest thing the boolean can say rather than to the loosest: a channel
    that asked for a doorman should not silently end up with none."""
    Stream = apps.get_model("zerver", "Stream")
    Stream.objects.exclude(call_door_policy=ANARCHY).update(require_moderator_to_join=True)


class Migration(migrations.Migration):
    dependencies = [
        ("zerver", "0815_alter_stream_can_create_rooms_group"),
    ]

    operations = [
        migrations.AddField(
            model_name="stream",
            name="call_door_policy",
            field=models.PositiveSmallIntegerField(default=ANARCHY, db_default=ANARCHY),
        ),
        migrations.RunPython(
            set_door_policy_from_boolean,
            reverse_code=set_boolean_from_door_policy,
            elidable=True,
        ),
        migrations.RemoveField(
            model_name="stream",
            name="require_moderator_to_join",
        ),
    ]
