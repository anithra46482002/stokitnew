from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_admin_password_flow")]
    operations = [
        migrations.CreateModel(
            name="AdminAccountChangeChallenge",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("principal_type", models.CharField(max_length=20)),
                ("principal_id", models.IntegerField(db_index=True)),
                ("purpose", models.CharField(max_length=32)),
                ("target_value", models.CharField(max_length=254, blank=True, default="")),
                ("destination", models.CharField(max_length=254)),
                ("otp_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("verified_at", models.DateTimeField(null=True, blank=True)),
                ("consumed_at", models.DateTimeField(null=True, blank=True)),
                ("invalidated_at", models.DateTimeField(null=True, blank=True)),
            ],
            options={"db_table": "accounts_adminaccountchangechallenge", "indexes": [models.Index(fields=["principal_type", "principal_id", "purpose", "created_at"], name="adm_acctchg_lookup_idx")]},
        ),
    ]
