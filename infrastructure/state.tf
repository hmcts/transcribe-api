terraform {
  # The CNP pipeline supplies the backend configuration (the shared
  # "subscription-tfstate" container and a state key derived from
  # product/component/env), so nothing is hard-coded here.
  backend "azurerm" {}
}
