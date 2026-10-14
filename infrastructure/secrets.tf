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
