from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def disable_voice_on_web_public_channels(
    apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """Calls are for a known set of people, so a channel that unauthenticated
    visitors can read does not get them. Channels allow calls by default, so any
    web-public channel that predates that rule is still opted in."""
    Stream = apps.get_model("zerver", "Stream")
    Stream.objects.filter(is_web_public=True, voice_video_enabled=True).update(
        voice_video_enabled=False
    )


class Migration(migrations.Migration):
    dependencies = [
        ("zerver", "0808_stream_voice_video_enabled"),
    ]

    operations = [
        migrations.RunPython(
            disable_voice_on_web_public_channels,
            reverse_code=migrations.RunPython.noop,
            elidable=True,
        ),
    ]
