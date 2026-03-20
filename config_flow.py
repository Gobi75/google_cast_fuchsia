"""Config flow for Continuously Cast Dashboards integration."""

import logging
import voluptuous as vol
from typing import Any
import copy
import datetime

from homeassistant.helpers import selector
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    DEFAULT_CAST_DELAY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_DELAY,
    DEFAULT_VERIFICATION_WAIT_TIME,
    DEFAULT_CASTING_TIMEOUT,
    DEFAULT_LOGGING_LEVEL,
    DEFAULT_START_TIME,
    DEFAULT_END_TIME,
    LOGGING_LEVELS,
)

_LOGGER = logging.getLogger(__name__)

# Subentry type for devices
SUBENTRY_TYPE_DEVICE = "device"


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Migrate an old config entry to a new version."""
    _LOGGER.info(f"Migrating config entry from version {config_entry.version}")

    if config_entry.version < 3:
        new_data = dict(config_entry.data)
        new_options = dict(config_entry.options)

        if "devices" in new_data:
            new_options["devices"] = new_data.pop("devices", {})

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options=new_options, version=3
        )

        _LOGGER.info("Configuration migration completed successfully")

    return True


class ContinuouslyCastingDashboardsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Continuously Cast Dashboards."""

    VERSION = 3

    def __init__(self):
        """Initialize the config flow."""
        self._devices = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        """Get the options flow for this handler."""
        return GlobalSettingsOptionsFlow(config_entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentry types supported by this integration."""
        return {SUBENTRY_TYPE_DEVICE: DeviceSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - global settings."""
        errors = {}

        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            try:
                cleaned_input = {}

                for key in ["logging_level", "cast_delay", "start_time", "end_time", "scan_interval", "max_retries", "casting_timeout"]:
                    if key in user_input and user_input[key] is not None:
                        cleaned_input[key] = user_input[key]

                if user_input.get("include_entity", False):
                    entity_id = user_input.get("switch_entity_id", "").strip()
                    if entity_id:
                        if self.hass and self.hass.states.get(entity_id) is None:
                            errors["switch_entity_id"] = "entity_not_found"
                        else:
                            cleaned_input["switch_entity_id"] = entity_id
                            entity_state = user_input.get(
                                "switch_entity_state", ""
                            ).strip()
                            if entity_state:
                                cleaned_input["switch_entity_state"] = entity_state

                if not errors:
                    return self.async_create_entry(
                        title="Continuously Casting Dashboards",
                        data=cleaned_input,
                    )
            except Exception as ex:
                _LOGGER.exception("Unexpected exception in user step: %s", ex)
                errors["base"] = "unknown"

        schema = vol.Schema(
            {
                vol.Required(
                    "logging_level", default=DEFAULT_LOGGING_LEVEL
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"label": "Debug", "value": "debug"},
                            {"label": "Info", "value": "info"},
                            {"label": "Warning", "value": "warning"},
                            {"label": "Error", "value": "error"},
                            {"label": "Critical", "value": "critical"},
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required("cast_delay", default=DEFAULT_CAST_DELAY): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=300, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required("scan_interval", default=DEFAULT_SCAN_INTERVAL): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=3600, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required("max_retries", default=DEFAULT_MAX_RETRIES): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=20, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required("retry_delay", default=DEFAULT_RETRY_DELAY): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=60, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required("casting_timeout", default=DEFAULT_CASTING_TIMEOUT): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=300, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    "start_time", default=DEFAULT_START_TIME
                ): selector.TimeSelector(),
                vol.Optional(
                    "end_time", default=DEFAULT_END_TIME
                ): selector.TimeSelector(),
                vol.Optional("include_entity", default=False): cv.boolean,
                vol.Optional("switch_entity_id", default=""): cv.string,
                vol.Optional("switch_entity_state", default=""): cv.string,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_import(
        self, import_config: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Import a config entry from YAML."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        data = {
            "logging_level": import_config.get("logging_level", DEFAULT_LOGGING_LEVEL),
            "cast_delay": import_config.get("cast_delay", DEFAULT_CAST_DELAY),
            "scan_interval": import_config.get("scan_interval", DEFAULT_SCAN_INTERVAL),
            "max_retries": import_config.get("max_retries", DEFAULT_MAX_RETRIES),
            "retry_delay": import_config.get("retry_delay", DEFAULT_RETRY_DELAY),
            "casting_timeout": import_config.get("casting_timeout", DEFAULT_CASTING_TIMEOUT),
            "start_time": import_config.get("start_time", DEFAULT_START_TIME),
            "end_time": import_config.get("end_time", DEFAULT_END_TIME),
        }

        if "switch_entity_id" in import_config:
            data["switch_entity_id"] = import_config["switch_entity_id"]

        if "switch_entity_state" in import_config:
            data["switch_entity_state"] = import_config["switch_entity_state"]

        # Store devices in options for migration
        options = {}
        if "devices" in import_config:
            options["devices"] = import_config["devices"]

        return self.async_create_entry(
            title="Continuously Casting Dashboards (imported)",
            data=data,
            options=options,
        )


class GlobalSettingsOptionsFlow(config_entries.OptionsFlow):
    """Handle global settings options flow."""

    def __init__(self, config_entry: ConfigEntry):
        """Initialize options flow."""
        super().__init__()
        self._entry = config_entry
        self._config = dict(config_entry.data)
        self._config.update(config_entry.options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the global settings options."""
        errors = {}

        if user_input is not None:
            try:
                cleaned_input = {}

                for key in ["logging_level", "cast_delay", "start_time", "end_time", "scan_interval", "max_retries", "casting_timeout"]:
                    if key in user_input and user_input[key] is not None:
                        cleaned_input[key] = user_input[key]

                if user_input.get("include_entity", False):
                    entity_id = user_input.get("switch_entity_id", "").strip()
                    if entity_id:
                        if self.hass and self.hass.states.get(entity_id) is None:
                            errors["switch_entity_id"] = "entity_not_found"
                        else:
                            cleaned_input["switch_entity_id"] = entity_id
                            entity_state = user_input.get(
                                "switch_entity_state", ""
                            ).strip()
                            if entity_state:
                                cleaned_input["switch_entity_state"] = entity_state

                if not errors:
                    # Preserve devices from options
                    devices = self._entry.options.get("devices", {})
                    new_options = {**cleaned_input, "devices": devices}

                    # Clean up empty entity fields
                    if "switch_entity_id" not in cleaned_input:
                        new_options.pop("switch_entity_id", None)
                        new_options.pop("switch_entity_state", None)

                    return self.async_create_entry(title="", data=new_options)

            except Exception as ex:
                _LOGGER.exception("Unexpected exception in options: %s", ex)
                errors["base"] = "unknown"

        has_entity = bool(self._config.get("switch_entity_id"))

        schema = vol.Schema(
            {
                vol.Required(
                    "logging_level",
                    default=self._config.get("logging_level", DEFAULT_LOGGING_LEVEL),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"label": "Debug", "value": "debug"},
                            {"label": "Info", "value": "info"},
                            {"label": "Warning", "value": "warning"},
                            {"label": "Error", "value": "error"},
                            {"label": "Critical", "value": "critical"},
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    "cast_delay",
                    default=self._config.get("cast_delay", DEFAULT_CAST_DELAY),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=300, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(
                    "scan_interval",
                    default=self._config.get("scan_interval", DEFAULT_SCAN_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=3600, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(
                    "max_retries",
                    default=self._config.get("max_retries", DEFAULT_MAX_RETRIES),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=20, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(
                    "casting_timeout",
                    default=self._config.get("casting_timeout", DEFAULT_CASTING_TIMEOUT),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=300, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    "start_time",
                    default=self._config.get("start_time", DEFAULT_START_TIME),
                ): selector.TimeSelector(),
                vol.Optional(
                    "end_time",
                    default=self._config.get("end_time", DEFAULT_END_TIME),
                ): selector.TimeSelector(),
                vol.Optional("include_entity", default=has_entity): cv.boolean,
                vol.Optional(
                    "switch_entity_id",
                    default=self._config.get("switch_entity_id", ""),
                ): cv.string,
                vol.Optional(
                    "switch_entity_state",
                    default=self._config.get("switch_entity_state", ""),
                ): cv.string,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device_count": str(len(self._entry.subentries)),
            },
        )


class DeviceSubentryFlow(ConfigSubentryFlow):
    """Handle device subentry flow - each device gets its own Configure button."""

    def __init__(self):
        """Initialize the subentry flow."""
        super().__init__()
        self._dashboards: list[dict] = []
        self._current_dashboard_index: int | None = None
        self._device_name: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle adding a new device."""
        errors = {}

        if user_input is not None:
            try:
                device_name = user_input.get("device_name", "").strip()

                if not device_name:
                    errors["device_name"] = "invalid_device_name"
                else:
                    # Check if device already exists as a subentry
                    for subentry in self._get_entry().subentries.values():
                        if subentry.data.get("device_name") == device_name:
                            errors["device_name"] = "device_already_exists"
                            break

                if not errors:
                    self._device_name = device_name
                    self._dashboards = []
                    return await self.async_step_add_dashboard()

            except Exception as ex:
                _LOGGER.exception("Error adding device: %s", ex)
                errors["base"] = "unknown"

        schema = vol.Schema(
            {
                vol.Required("device_name"): cv.string,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_add_dashboard(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle adding a dashboard to the device."""
        errors = {}

        if user_input is not None:
            try:
                cleaned_input = {}

                dashboard_url = user_input.get("dashboard_url", "").strip()
                if not dashboard_url:
                    errors["dashboard_url"] = "missing_dashboard_url"
                else:
                    cleaned_input["dashboard_url"] = dashboard_url

                # CHECKBOX LOGIC: Record volume only if override is checked
                if user_input.get("override_volume"):
                    cleaned_input["volume"] = user_input.get("volume")
                    cleaned_input["override_volume"] = True
                else:
                    cleaned_input["volume"] = None
                    cleaned_input["override_volume"] = False

                if user_input.get("enable_time_window", False):
                    if user_input.get("start_time"):
                        cleaned_input["start_time"] = user_input["start_time"]
                    if user_input.get("end_time"):
                        cleaned_input["end_time"] = user_input["end_time"]

                if user_input.get("include_entity", False):
                    entity_id = user_input.get("switch_entity_id", "").strip()
                    if entity_id:
                        if self.hass and self.hass.states.get(entity_id) is None:
                            errors["switch_entity_id"] = "entity_not_found"
                        else:
                            cleaned_input["switch_entity_id"] = entity_id
                            entity_state = user_input.get("switch_entity_state", "").strip()
                            if entity_state:
                                cleaned_input["switch_entity_state"] = entity_state

                if user_input.get("include_speaker_groups", False):
                    speaker_groups_input = user_input.get("speaker_groups", "").strip()
                    if speaker_groups_input:
                        speaker_groups = [g.strip() for g in speaker_groups_input.split(",") if g.strip()]
                        if speaker_groups:
                            cleaned_input["speaker_groups"] = speaker_groups

                if not errors:
                    self._dashboards.append(cleaned_input)
                    if user_input.get("add_another", False):
                        return await self.async_step_add_dashboard()
                    else:
                        return self._create_device_entry()

            except Exception as ex:
                _LOGGER.exception("Error adding dashboard: %s", ex)
                errors["base"] = "unknown"

        #Form with added override volume checkbox
        entry = self._get_entry()
        global_config = dict(entry.data)
        global_config.update(entry.options)

        schema = vol.Schema(
            {
                vol.Required("dashboard_url"): cv.string,
                vol.Optional("override_volume", default=False): cv.boolean,
                vol.Optional("volume", default=5): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=100)
                ),
                vol.Optional("enable_time_window", default=False): cv.boolean,
                vol.Optional("start_time", default=global_config.get("start_time", DEFAULT_START_TIME)): selector.TimeSelector(),
                vol.Optional("end_time", default=global_config.get("end_time", DEFAULT_END_TIME)): selector.TimeSelector(),
                vol.Optional("include_entity", default=False): cv.boolean,
                vol.Optional("switch_entity_id", default=""): cv.string,
                vol.Optional("switch_entity_state", default=""): cv.string,
                vol.Optional("include_speaker_groups", default=False): cv.boolean,
                vol.Optional("speaker_groups", default=""): cv.string,
                vol.Optional("add_another", default=False): cv.boolean,
            }
        )

        return self.async_show_form(
            step_id="add_dashboard",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device_name": self._device_name,
                "dashboard_count": str(len(self._dashboards)),
            },
        )

    def _create_device_entry(self) -> SubentryFlowResult:
        """Create the device subentry."""
        # Clean up dashboards
        cleaned_dashboards = []
        for dashboard in self._dashboards:
            cleaned = {}
            for key, value in dashboard.items():
                if isinstance(value, (datetime.datetime, datetime.time)):
                    continue
                if key in ["switch_entity_id", "switch_entity_state"]:
                    if value and str(value).strip():
                        cleaned[key] = value
                elif key == "speaker_groups":
                    if value and isinstance(value, list) and any(value):
                        cleaned[key] = value
                else:
                    cleaned[key] = value
            cleaned_dashboards.append(cleaned)

        return self.async_create_entry(
            title=self._device_name,
            data={
                "device_name": self._device_name,
                "dashboards": cleaned_dashboards,
            },
        )

    # ============ RECONFIGURE FLOW (for editing existing devices) ============

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguring an existing device subentry.

        Shows a unified settings form with all device configuration.
        """
        # Load existing data from the subentry being reconfigured
        subentry = self._get_reconfigure_subentry()
        self._device_name = subentry.data.get("device_name", "")
        self._dashboards = copy.deepcopy(subentry.data.get("dashboards", []))

        # If device has exactly one dashboard, go directly to edit it
        if len(self._dashboards) == 1:
            self._current_dashboard_index = 0
            return await self.async_step_reconfigure_device()
        elif len(self._dashboards) == 0:
            # No dashboards, go to add one
            return await self.async_step_reconfigure_add_dashboard()
        else:
            # Multiple dashboards, show selection menu
            return await self.async_step_reconfigure_select_dashboard()

    async def async_step_reconfigure_select_dashboard(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Select which dashboard to edit when device has multiple dashboards."""
        errors = {}

        if user_input is not None:
            action = user_input.get("dashboard_action")

            if action == "add_dashboard":
                return await self.async_step_reconfigure_add_dashboard()
            elif action and action.startswith("edit:"):
                self._current_dashboard_index = int(action.split(":")[1])
                return await self.async_step_reconfigure_device()
            elif action and action.startswith("delete:"):
                index = int(action.split(":")[1])
                if 0 <= index < len(self._dashboards):
                    self._dashboards.pop(index)
                    # If only one left, go to edit it
                    if len(self._dashboards) == 1:
                        self._current_dashboard_index = 0
                        return await self.async_step_reconfigure_device()
                    elif len(self._dashboards) == 0:
                        return await self.async_step_reconfigure_add_dashboard()
                return await self.async_step_reconfigure_select_dashboard()

        # Build options showing all dashboards
        options = []

        for i, dashboard in enumerate(self._dashboards):
            url = dashboard.get("dashboard_url", "Unknown")
            # Extract just the path for cleaner display
            if "://" in url:
                url_path = url.split("://", 1)[1]
                if "/" in url_path:
                    url_path = "/" + url_path.split("/", 1)[1]
                else:
                    url_path = url
            else:
                url_path = url
            display_url = url_path[:40] + "..." if len(url_path) > 40 else url_path

            info_parts = []
            if dashboard.get("volume") is not None:
                info_parts.append(f"Vol:{dashboard.get('volume')}")
            if dashboard.get("start_time") or dashboard.get("end_time"):
                start = dashboard.get("start_time", "")
                end = dashboard.get("end_time", "")
                if start or end:
                    info_parts.append(f"{start}-{end}")

            info_str = f" ({', '.join(info_parts)})" if info_parts else ""

            options.append({
                "label": f"Dashboard {i + 1}: {display_url}{info_str}",
                "value": f"edit:{i}",
            })

        options.append({"label": "Add another dashboard", "value": "add_dashboard"})

        # Add delete options at the end
        for i in range(len(self._dashboards)):
            options.append({
                "label": f"Delete dashboard {i + 1}",
                "value": f"delete:{i}",
            })

        schema = vol.Schema(
            {
                vol.Required("dashboard_action"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="reconfigure_select_dashboard",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device_name": self._device_name,
                "dashboard_count": str(len(self._dashboards)),
            },
        )

    async def async_step_reconfigure_device(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Unified device settings form - all settings in one place."""
        errors = {}

        # Get current dashboard data
        if self._current_dashboard_index is not None and self._current_dashboard_index < len(self._dashboards):
            current = self._dashboards[self._current_dashboard_index]
        else:
            current = {}

        if user_input is not None:
            try:
                add_another = user_input.get("add_another_dashboard", False)
                delete_this = user_input.get("delete_this_dashboard", False)
                change_dashboard = user_input.get("change_dashboard", False)

                if delete_this:
                    if len(self._dashboards) > 1 and self._current_dashboard_index is not None:
                        self._dashboards.pop(self._current_dashboard_index)
                        self._current_dashboard_index = 0
                        return await self.async_step_reconfigure_device()
                    errors["base"] = "cannot_delete_last"

                if change_dashboard:
                    # Save without validation and go to dashboard selection
                    self._save_dashboard_from_input(user_input, current)
                    return await self.async_step_reconfigure_select_dashboard()

                # Save action - validate and save
                cleaned_input = {}

                # Device name (can be changed inline)
                new_device_name = user_input.get("device_name", "").strip()
                if not new_device_name:
                    errors["device_name"] = "invalid_device_name"
                elif new_device_name != self._device_name:
                    # Check if name already exists
                    entry = self._get_entry()
                    current_subentry = self._get_reconfigure_subentry()
                    for sid, subentry in entry.subentries.items():
                        if (
                            sid != current_subentry.subentry_id
                            and subentry.data.get("device_name") == new_device_name
                        ):
                            errors["device_name"] = "device_already_exists"
                            break
                    if not errors:
                        self._device_name = new_device_name

                # Dashboard URL
                dashboard_url = user_input.get("dashboard_url", "").strip()
                if not dashboard_url:
                    errors["dashboard_url"] = "missing_dashboard_url"
                else:
                    cleaned_input["dashboard_url"] = dashboard_url

                # CHANGE: Record volume only if checkbox is checked
                if user_input.get("override_volume"):
                    cleaned_input["volume"] = user_input.get("volume")
                    cleaned_input["override_volume"] = True
                else:
                    cleaned_input["volume"] = None
                    cleaned_input["override_volume"] = False

                if user_input.get("enable_time_window", False):
                    if user_input.get("start_time"):
                        cleaned_input["start_time"] = user_input["start_time"]
                    if user_input.get("end_time"):
                        cleaned_input["end_time"] = user_input["end_time"]

                if user_input.get("include_entity", False):
                    entity_id = user_input.get("switch_entity_id", "").strip()
                    if entity_id:
                        if self.hass and self.hass.states.get(entity_id) is None:
                            errors["switch_entity_id"] = "entity_not_found"
                        else:
                            cleaned_input["switch_entity_id"] = entity_id
                            entity_state = user_input.get(
                                "switch_entity_state", ""
                            ).strip()
                            if entity_state:
                                cleaned_input["switch_entity_state"] = entity_state

                if user_input.get("include_speaker_groups", False):
                    speaker_groups_input = user_input.get("speaker_groups", "").strip()
                    if speaker_groups_input:
                        speaker_groups = [
                            g.strip()
                            for g in speaker_groups_input.split(",")
                            if g.strip()
                        ]
                        if speaker_groups:
                            cleaned_input["speaker_groups"] = speaker_groups

                if not errors:
                    # Update the dashboard
                    if self._current_dashboard_index is not None and self._current_dashboard_index < len(self._dashboards):
                        self._dashboards[self._current_dashboard_index] = cleaned_input
                    else:
                        self._dashboards.append(cleaned_input)

                    if add_another:
                        return await self.async_step_reconfigure_add_dashboard()

                    return self._save_reconfigure()

            except Exception as ex:
                _LOGGER.exception("Error in device settings: %s", ex)
                errors["base"] = "unknown"

        # Build the form with current values
        speaker_groups = current.get("speaker_groups", [])
        speaker_groups_str = (
            ", ".join(speaker_groups) if isinstance(speaker_groups, list) else ""
        )

        has_time = bool(current.get("start_time") or current.get("end_time"))
        has_entity = bool(current.get("switch_entity_id"))
        has_groups = bool(current.get("speaker_groups"))
        
        # CHANGE: Check if the volume is currently set (or not None)
        is_volume_set = current.get("volume") is not None

        # Get global settings for defaults
        entry = self._get_entry()
        global_config = dict(entry.data)
        global_config.update(entry.options)

        schema_fields = {
            vol.Required(
                "device_name", default=self._device_name
            ): cv.string,
            vol.Required(
                "dashboard_url", default=current.get("dashboard_url", "")
            ): cv.string,
            # CHANGE: New checkbox for volume control
            vol.Optional(
                "override_volume", default=is_volume_set
            ): cv.boolean,
            vol.Required(
                "volume", default=current.get("volume") or 5
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
            vol.Optional("enable_time_window", default=has_time): cv.boolean,
            vol.Optional(
                "start_time",
                default=current.get(
                    "start_time",
                    global_config.get("start_time", DEFAULT_START_TIME),
                ),
            ): selector.TimeSelector(),
            vol.Optional(
                "end_time",
                default=current.get(
                    "end_time",
                    global_config.get("end_time", DEFAULT_END_TIME),
                ),
            ): selector.TimeSelector(),
            vol.Optional("include_entity", default=has_entity): cv.boolean,
            vol.Optional(
                "switch_entity_id",
                default=current.get("switch_entity_id", ""),
            ): cv.string,
            vol.Optional(
                "switch_entity_state",
                default=current.get("switch_entity_state", ""),
            ): cv.string,
            vol.Optional("include_speaker_groups", default=has_groups): cv.boolean,
            vol.Optional("speaker_groups", default=speaker_groups_str): cv.string,
        }

        if len(self._dashboards) > 1:
            schema_fields[vol.Optional("change_dashboard", default=False)] = cv.boolean
            schema_fields[vol.Optional("delete_this_dashboard", default=False)] = cv.boolean

        schema_fields[vol.Optional("add_another_dashboard", default=False)] = cv.boolean

        schema = vol.Schema(schema_fields)

        dashboard_info = ""
        if len(self._dashboards) > 1:
            dashboard_info = f" (Dashboard {self._current_dashboard_index + 1} of {len(self._dashboards)})"

        return self.async_show_form(
            step_id="reconfigure_device",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device_name": self._device_name,
                "dashboard_info": dashboard_info,
            },
        )

    def _save_dashboard_from_input(self, user_input: dict[str, Any], current: dict) -> None:
        """Helper to save dashboard data from form input without validation."""
        cleaned_input = {}

        dashboard_url = user_input.get("dashboard_url", "").strip()
        if dashboard_url:
            cleaned_input["dashboard_url"] = dashboard_url

        # CHANGE: We are checking our new 'override volume' checkbox
        if user_input.get("override_volume"):
            cleaned_input["volume"] = user_input.get("volume")
            cleaned_input["override_volume"] = True
        else:
            cleaned_input["volume"] = None
            cleaned_input["override_volume"] = False

        if user_input.get("enable_time_window", False):
            if user_input.get("start_time"):
                cleaned_input["start_time"] = user_input["start_time"]
            if user_input.get("end_time"):
                cleaned_input["end_time"] = user_input["end_time"]

        if user_input.get("include_entity", False):
            entity_id = user_input.get("switch_entity_id", "").strip()
            if entity_id:
                cleaned_input["switch_entity_id"] = entity_id
                entity_state = user_input.get("switch_entity_state", "").strip()
                if entity_state:
                    cleaned_input["switch_entity_state"] = entity_state

        if user_input.get("include_speaker_groups", False):
            speaker_groups_input = user_input.get("speaker_groups", "").strip()
            if speaker_groups_input:
                speaker_groups = [g.strip() for g in speaker_groups_input.split(",") if g.strip()]
                if speaker_groups:
                    cleaned_input["speaker_groups"] = speaker_groups

        # Update device name if provided
        new_device_name = user_input.get("device_name", "").strip()
        if new_device_name:
            self._device_name = new_device_name

        # Update the dashboard
        if cleaned_input.get("dashboard_url"):
            if self._current_dashboard_index is not None and self._current_dashboard_index < len(self._dashboards):
                self._dashboards[self._current_dashboard_index] = cleaned_input

    async def async_step_reconfigure_rename(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle renaming a device during reconfiguration."""
        errors = {}

        if user_input is not None:
            try:
                new_name = user_input.get("new_device_name", "").strip()

                if not new_name:
                    errors["new_device_name"] = "invalid_device_name"
                elif new_name != self._device_name:
                    # Check if name already exists in other subentries
                    entry = self._get_entry()
                    current_subentry = self._get_reconfigure_subentry()
                    for sid, subentry in entry.subentries.items():
                        if (
                            sid != current_subentry.subentry_id
                            and subentry.data.get("device_name") == new_name
                        ):
                            errors["new_device_name"] = "device_already_exists"
                            break

                if not errors:
                    self._device_name = new_name
                    return await self.async_step_reconfigure_device()

            except Exception as ex:
                _LOGGER.exception("Error renaming device: %s", ex)
                errors["base"] = "unknown"

        schema = vol.Schema(
            {
                vol.Required("new_device_name", default=self._device_name): cv.string,
            }
        )

        return self.async_show_form(
            step_id="reconfigure_rename",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reconfigure_add_dashboard(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle adding a new dashboard during reconfiguration."""
        errors = {}

        if user_input is not None:
            try:
                cleaned_input = {}

                dashboard_url = user_input.get("dashboard_url", "").strip()
                if not dashboard_url:
                    errors["dashboard_url"] = "missing_dashboard_url"
                else:
                    cleaned_input["dashboard_url"] = dashboard_url

                if user_input.get("volume") is not None:
                    cleaned_input["volume"] = user_input["volume"]

                if user_input.get("enable_time_window", False):
                    if user_input.get("start_time"):
                        cleaned_input["start_time"] = user_input["start_time"]
                    if user_input.get("end_time"):
                        cleaned_input["end_time"] = user_input["end_time"]

                if user_input.get("include_entity", False):
                    entity_id = user_input.get("switch_entity_id", "").strip()
                    if entity_id:
                        if self.hass and self.hass.states.get(entity_id) is None:
                            errors["switch_entity_id"] = "entity_not_found"
                        else:
                            cleaned_input["switch_entity_id"] = entity_id
                            entity_state = user_input.get(
                                "switch_entity_state", ""
                            ).strip()
                            if entity_state:
                                cleaned_input["switch_entity_state"] = entity_state

                if user_input.get("include_speaker_groups", False):
                    speaker_groups_input = user_input.get("speaker_groups", "").strip()
                    if speaker_groups_input:
                        speaker_groups = [
                            g.strip()
                            for g in speaker_groups_input.split(",")
                            if g.strip()
                        ]
                        if speaker_groups:
                            cleaned_input["speaker_groups"] = speaker_groups

                if not errors:
                    self._dashboards.append(cleaned_input)
                    # Set the new dashboard as current and go to the device form
                    self._current_dashboard_index = len(self._dashboards) - 1
                    return self._save_reconfigure()

            except Exception as ex:
                _LOGGER.exception("Error adding dashboard: %s", ex)
                errors["base"] = "unknown"

        # Get global settings for defaults
        entry = self._get_entry()
        global_config = dict(entry.data)
        global_config.update(entry.options)

        schema = vol.Schema(
            {
                vol.Required("dashboard_url"): cv.string,
                vol.Optional("volume", default=5): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=100)
                ),
                vol.Optional("enable_time_window", default=False): cv.boolean,
                vol.Optional(
                    "start_time",
                    default=global_config.get("start_time", DEFAULT_START_TIME),
                ): selector.TimeSelector(),
                vol.Optional(
                    "end_time",
                    default=global_config.get("end_time", DEFAULT_END_TIME),
                ): selector.TimeSelector(),
                vol.Optional("include_entity", default=False): cv.boolean,
                vol.Optional("switch_entity_id", default=""): cv.string,
                vol.Optional("switch_entity_state", default=""): cv.string,
                vol.Optional("include_speaker_groups", default=False): cv.boolean,
                vol.Optional("speaker_groups", default=""): cv.string,
            }
        )

        return self.async_show_form(
            step_id="reconfigure_add_dashboard",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device_name": self._device_name,
            },
        )

    def _save_reconfigure(self) -> SubentryFlowResult:
        """Save the reconfigured device."""
        # Clean up dashboards
        cleaned_dashboards = []
        for dashboard in self._dashboards:
            cleaned = {}
            for key, value in dashboard.items():
                if isinstance(value, (datetime.datetime, datetime.time)):
                    continue
                if key in ["switch_entity_id", "switch_entity_state"]:
                    if value and str(value).strip():
                        cleaned[key] = value
                elif key == "speaker_groups":
                    if value and isinstance(value, list) and any(value):
                        cleaned[key] = value
                else:
                    cleaned[key] = value
            cleaned_dashboards.append(cleaned)

        return self.async_update_and_abort(
            self._get_entry(),
            self._get_reconfigure_subentry(),
            title=self._device_name,
            data={
                "device_name": self._device_name,
                "dashboards": cleaned_dashboards,
            },
        )
