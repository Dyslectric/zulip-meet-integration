"""Calls exist only on voice channels, and being one is opt-in.

voice_video_enabled previously meant "calls are permitted here" and defaulted to
True, so every channel allowed them. It now means "this is a voice channel":
it shows the channel as a speaker, pins it single-threaded, and is the only
state in which calls can be started. Existing channels are not voice channels,
so this turns calls off across the board — they come back per channel, by
opting in.

text_chat_disabled is the second half of that: a voice channel is either
single-threaded or carries no text conversation at all.
"""

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def no_existing_channel_is_a_voice_channel(
    apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    Stream = apps.get_model("zerver", "Stream")
    Stream.objects.filter(voice_video_enabled=True).update(voice_video_enabled=False)


class Migration(migrations.Migration):

    dependencies = [
        ("zerver", "0809_disable_voice_on_web_public_channels"),
    ]

    operations = [
        migrations.AddField(
            model_name="stream",
            name="text_chat_disabled",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AlterField(
            model_name="stream",
            name="voice_video_enabled",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.RunPython(
            no_existing_channel_is_a_voice_channel,
            reverse_code=migrations.RunPython.noop,
            elidable=False,
        ),
    ]
