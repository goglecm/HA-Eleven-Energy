"""Names vulture cannot see being used, each with who uses it.

Run as ``vulture custom_components tests vulture_whitelist.py --min-confidence 60``.
Every entry is called or read by Home Assistant or pytest, never by our own
code, so a static scan reports it as unused. Anything not listed here that
vulture reports is dead code and is deleted.
"""

# ruff: noqa: B018
# These are bare expressions on purpose: vulture counts any mention as a use.

_ = object()

# --- Home Assistant integration hooks (loaded by name) ---------------------
_.CONFIG_SCHEMA  # __init__.py: YAML schema; config entries only
_.async_setup_entry  # __init__.py and each platform module
_.async_unload_entry  # __init__.py
_.async_remove_entry  # __init__.py
_.async_remove_config_entry_device  # __init__.py

# --- Config flow: class registered by domain, steps called by step id -----
_.ConfigFlow
_.VERSION
_.logger  # required by AbstractOAuth2FlowHandler
_.async_step_user
_.async_step_token
_.async_step_reconfigure
_.async_step_reauth
_.async_step_reauth_account
_.async_step_reauth_confirm
_.async_step_init
_.async_oauth_create_entry
_.async_get_options_flow

# --- Device-action protocol --------------------------------------------------
_.async_validate_action_config
_.async_get_actions
_.async_call_action_from_config
_.async_get_action_capabilities
_.variables  # a required parameter of async_call_action_from_config

# --- Entity attributes Home Assistant reads -------------------------------
_._attr_device_info
_._attr_translation_key
_._attr_translation_placeholders
_._attr_name
_._attr_icon
_._attr_entity_category
_._attr_native_unit_of_measurement
_._attr_device_class
_._attr_state_class
_._attr_suggested_display_precision
_.async_entity_id_changed  # Home Assistant 2026.11+ calls it on a rename
_.old_entity_id  # its required parameter
_.entity_registry_id  # StoredState field read by 2026.11 restore (tests/common.py)

# --- pytest hooks and fixtures (tests/conftest.py) -----------------------
_.pytest_plugins
_.pytest_addoption
_.pytest_collection_modifyitems
_.pytest_runtest_logreport
_.pytest_sessionfinish
_.exitstatus  # a required parameter of pytest_sessionfinish
_.auto_enable_custom_integrations
_.enable_custom_integrations  # requested by the autouse fixture above
_.fail_on_ha_usage_reports
_.mock_setup_entry
