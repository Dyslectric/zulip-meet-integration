import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Room knock/invite settings, the moderator door policy, and the group that
    says who may start a room.

    can_create_rooms_group is added nullable here and made non-nullable two
    migrations later, with the backfill in between — the standard three-step for
    a non-nullable foreign key on a table that already has rows.
    """

    dependencies = [
        ("zerver", "0812_lounge_room"),
    ]

    operations = [
        migrations.AddField(
            model_name="loungeroom",
            name="knockable_by_users",
            field=models.BooleanField(db_default=True, default=True),
        ),
        migrations.AddField(
            model_name="loungeroom",
            name="knockable_by_guests",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name="loungeroom",
            name="invited_users",
            field=models.ManyToManyField(related_name="+", to="zerver.userprofile"),
        ),
        migrations.AddField(
            model_name="stream",
            name="require_moderator_to_join",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name="stream",
            name="can_create_rooms_group",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="+",
                to="zerver.usergroup",
            ),
        ),
    ]
