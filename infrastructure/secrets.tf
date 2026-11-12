# Fernet key for stored webhook secrets: 43 url-safe base64 characters plus "=".
# random_password because it is approved for this repo and its result is
# sensitive. Replacing it makes existing encrypted webhook secrets unreadable.
resource "random_password" "webhook_secret_encryption_key" {
  length           = 43
  special          = true
  override_special = "-_"
}

resource "azurerm_key_vault_secret" "webhook_secret_encryption_key" {
  name         = "webhook-secret-encryption-key"
  value        = "${random_password.webhook_secret_encryption_key.result}="
  key_vault_id = data.azurerm_key_vault.transcribe.id
}

# HS256 key for the BenchNotes access tokens (APP_JWT_SIGNING_KEY, at least 32
# characters). Replacing it signs every user out.
resource "random_password" "app_jwt_signing_key" {
  length           = 64
  special          = true
  override_special = "-_"
}

resource "azurerm_key_vault_secret" "app_jwt_signing_key" {
  name         = "app-jwt-signing-key"
  value        = random_password.app_jwt_signing_key.result
  key_vault_id = data.azurerm_key_vault.transcribe.id
}
