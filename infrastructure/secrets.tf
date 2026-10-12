# Fernet key for encrypting stored webhook secrets (runtime/security.py). It
# is ours alone, not a credential for another system, so it is generated here
# rather than seeded by hand. A Fernet key is 32 bytes, url-safe base64 encoded:
# 43 characters from that alphabet plus one "=" of padding. random_password is
# used because it is the random resource CNP approves per repo
# (cnp-jenkins-config terraform-infra-approvals) and its result is sensitive;
# random_string is approved globally but its id is the value, which would print
# the key in the apply log. With override_special = "-_" every character is in
# the url-safe base64 alphabet, giving 256 bits of key. The value is kept in
# Terraform state, so it does not change between runs; rotating it means
# replacing random_password.webhook_secret_encryption_key, after which webhook
# secrets encrypted under the old key no longer decrypt.
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
