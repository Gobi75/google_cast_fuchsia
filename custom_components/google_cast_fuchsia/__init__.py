"""Continuously Cast Dashboards"""

import logging
import asyncio
import os
from datetime import timedelta
import voluptuous as vol
import json

from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_DEVICES, CONF_SCAN_INTERVAL
from homeassistant.helpers.event import async_track_time_interval
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT, ConfigSubentry
from homeassistant.helpers.storage import Store
import homeassistant.helpers.config_validation as cv

from .casting import CastingManager
from .device import DeviceManager
from .monitoring import MonitoringManager
from .stats import StatsManager
from .utils import TimeWindowChecker, SwitchEntityChecker
from .config_flow import async_migrate_entry
from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_CAST_DELAY,
    CONF_CASTING_TIMEOUT,
    CONF_LOGGING_LEVEL,
    CONF_RETRY_DELAY,
    DEFAULT_CAST_DELAY,
    DEFAULT_LOGGING_LEVEL,
    DEFAULT_START_TIME,
    DEFAULT_END_TIME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_CASTING_TIMEOUT,
    DEFAULT_RETRY_DELAY,
)

_LOGGER = logging.getLogger(__name__)

# Global lock to prevent concurrent setup of the same entry
_SETUP_LOCKS = {}

async def _read_notification_state(hass: HomeAssistant, storage_file: str) -> bool:
    """Read notification state without blocking the event loop."""
    def _read():
        if not os.path.exists(storage_file):
            return False
        with open(storage_file, "r") as f:
            data = json.load(f)
            return data.get("acknowledged", False)

    try:
        return await hass.async_add_executor_job(_read)
    except Exception as ex:
        _LOGGER.debug("Error loading notification state: %s", ex)
        return False

async def _write_notification_state(hass: HomeAssistant, storage_file: str, acknowledged: bool) -> None:
    """Write notification state without blocking the event loop."""
    def _write():
        with open(storage_file, "w") as f:
            json.dump({"acknowledged": acknowledged}, f)

    try:
        await hass.async_add_executor_job(_write)
    except Exception as ex:
        _LOGGER.debug("Failed to save acknowledged state: %s", ex)

async def _async_forward_entry_setup(hass: HomeAssistant, entry: ConfigEntry, platform: str) -> None:
    """Forward entry setup using the correct Home Assistant API."""
    forward_setups = getattr(hass.config_entries, "async_forward_entry_setups", None)
    if forward_setups:
        await forward_setups(entry, [platform])
        return
    await hass.config_entries.async_forward_entry_setup(entry, platform)

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Continuously Cast Dashboards component."""
    hass.data.setdefault(DOMAIN, {})

    # Simple file-based approach to track notification state
    storage_file = hass.config.path(f".{DOMAIN}_notification_state.json")
    _LOGGER.debug(f"Using storage file at: {storage_file}")

    notification_shown = await _read_notification_state(hass, storage_file)

    if DOMAIN in config:
        _LOGGER.debug("Found YAML configuration for Continuously Cast Dashboards")

        # Check if we already have config entries for this domain to avoid conflicts
        existing_entries = [entry for entry in hass.config_entries.async_entries(DOMAIN)]
        if existing_entries:
            _LOGGER.warning("Config entries already exist for %s, skipping YAML import to avoid conflicts", DOMAIN)
            _LOGGER.info("But will attempt to import YAML devices into existing entry...")

            # Try to import YAML devices into the existing entry
            await _import_yaml_devices_to_existing_entry(hass, existing_entries[0], config[DOMAIN])
            return True

        # If notification hasn't been shown yet
        if not notification_shown:
            # Create persistent notification
            notification_id = f"{DOMAIN}_config_imported"
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Continuously Cast Dashboards Configuration Imported",
                    "message": (
                        "Your YAML configuration for Continuously Cast Dashboards has been imported into the UI configuration.\n\n"
                        "Please remove the configuration from your configuration.yaml file to avoid conflicts.\n\n"
                        "You can now manage your configuration through the UI. "
                        "Click DISMISS to prevent this message from appearing again."
                    ),
                    "notification_id": notification_id,
                },
            )

            # Log all events to see what's happening
            async def log_all_events(event):
                """Log all events to see what's happening."""
                # Any event that looks related to notifications
                if "notification" in event.event_type.lower():
                    # See if our notification_id appears anywhere in the event data
                    event_data_str = str(event.data)
                    if notification_id in event_data_str:
                        # Save the acknowledged state regardless of the exact event type
                        await _write_notification_state(hass, storage_file, True)

            # Listen for ALL events for diagnostic purposes
            remove_listener = hass.bus.async_listen("*", log_all_events)

            # Store the listener so it doesn't get garbage collected
            hass.data[DOMAIN]["remove_listener"] = remove_listener

            # Also create a one-time task to auto-acknowledge after 5 minutes
            # as a fallback in case the event system isn't working
            async def auto_acknowledge():
                """Automatically acknowledge after a timeout."""
                import asyncio
                await asyncio.sleep(300)  # 5 minutes

                # Check if we've already acknowledged
                acknowledged = await _read_notification_state(hass, storage_file)
                if acknowledged:
                    return  # Already acknowledged, nothing to do

                # Not acknowledged yet, do it now
                _LOGGER.debug("Auto-acknowledging notification after timeout")
                await _write_notification_state(hass, storage_file, True)

            # Start the auto-acknowledge task
            hass.async_create_task(auto_acknowledge())
        else:
            _LOGGER.debug("Notification was previously acknowledged, skipping")

        # Forward the YAML config to the config flow
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data=config[DOMAIN],
            )
        )

    return True


async def _import_yaml_devices_to_existing_entry(hass: HomeAssistant, entry: ConfigEntry, yaml_config: dict) -> None:
    """Import devices from YAML into an existing config entry."""
    devices = yaml_config.get("devices", {})
    if not devices:
        _LOGGER.info("No devices found in YAML configuration")
        return

    _LOGGER.info("Found %d devices in YAML, importing into existing entry", len(devices))

    # Get current options
    current_options = dict(entry.options)

    # Add YAML devices to options (preserving any existing devices)
    if "devices" not in current_options:
        current_options["devices"] = {}

    # Check if devices are already in options
    existing_device_names = set(current_options["devices"].keys())
    new_devices = {k: v for k, v in devices.items() if k not in existing_device_names}

    if not new_devices:
        _LOGGER.info("All YAML devices already in options")
        return

    current_options["devices"].update(new_devices)

    # Update the entry
    hass.config_entries.async_update_entry(entry, options=current_options)

    # Show notification
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": "YAML Devices Imported - Reload Required",
            "message": (
                f"Imported {len(new_devices)} device(s) from YAML:\n"
                f"{', '.join(new_devices.keys())}\n\n"
                "**Important:** Please reload this integration:\n"
                "1. Go to Settings → Devices & services → Continuously Cast Dashboards\n"
                "2. Click the 3 dots → Reload\n\n"
                "After reload, your devices will be available and migration can proceed."
            ),
            "notification_id": f"{DOMAIN}_yaml_import",
        },
    )

    _LOGGER.info("Successfully imported %d devices from YAML: %s", len(new_devices), list(new_devices.keys()))

def _get_legacy_devices(entry: ConfigEntry) -> tuple[dict, str]:
    """Return legacy devices and their source (options|data|none)."""
    legacy_devices = entry.options.get("devices", {})
    if legacy_devices:
        return legacy_devices, "options"

    legacy_devices = entry.data.get("devices", {})
    if legacy_devices:
        return legacy_devices, "data"

    return {}, "none"

def _build_devices_from_subentries(entry: ConfigEntry) -> dict:
    """Build devices dict from subentries for backward compatibility."""
    devices = {}

    # First, check if there are legacy devices in options (for migration)
    legacy_devices, legacy_source = _get_legacy_devices(entry)
    if legacy_devices:
        _LOGGER.debug(
            "Found legacy devices in %s: %s",
            legacy_source,
            list(legacy_devices.keys()),
        )
        devices.update(legacy_devices)

    # Then, add/override with subentry devices
    for subentry_id, subentry in entry.subentries.items():
        device_name = subentry.data.get("device_name")
        dashboards = subentry.data.get("dashboards", [])
        if device_name:
            _LOGGER.debug("Found subentry device: %s with %d dashboards", device_name, len(dashboards))
            devices[device_name] = dashboards

    return devices


async def _migrate_legacy_devices_to_subentries(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate legacy devices from options to subentries using HA's config entry API.

    Returns True if migration was performed and a restart is needed.
    """
    from .config_flow import SUBENTRY_TYPE_DEVICE

    _LOGGER.info("=== CHECKING FOR LEGACY DEVICE MIGRATION ===")
    _LOGGER.debug("entry.options keys: %s", list(entry.options.keys()))
    _LOGGER.debug("entry.data keys: %s", list(entry.data.keys()))
    _LOGGER.debug("Current subentries count: %d", len(entry.subentries))

    # Check if there are legacy devices that need migration
    legacy_devices, legacy_source = _get_legacy_devices(entry)

    if not legacy_devices:
        _LOGGER.info("No legacy devices found in entry %s to migrate", legacy_source)
        return False

    _LOGGER.info("Found %d legacy devices in %s: %s", len(legacy_devices), legacy_source, list(legacy_devices.keys()))

    # Get existing subentry device names to avoid duplicates
    subentry_device_names = {
        subentry.data.get("device_name")
        for subentry in entry.subentries.values()
        if subentry.data.get("device_name")
    }
    _LOGGER.debug("Existing subentry devices: %s", subentry_device_names)

    # Find devices that haven't been migrated yet
    devices_to_migrate = {
        name: dashboards
        for name, dashboards in legacy_devices.items()
        if name not in subentry_device_names
    }

    if not devices_to_migrate:
        _LOGGER.info("All legacy devices already have subentries, cleaning up legacy storage...")
        # Clean up legacy devices from options/data since they're already migrated
        new_options = dict(entry.options)
        new_data = dict(entry.data)
        if legacy_source == "options":
            new_options.pop("devices", None)
        elif legacy_source == "data":
            new_data.pop("devices", None)
        hass.config_entries.async_update_entry(entry, options=new_options, data=new_data)
        return False

    _LOGGER.info("Will migrate %d devices to subentries: %s", len(devices_to_migrate), list(devices_to_migrate.keys()))

    # Use Home Assistant's proper subentry creation API
    migrated_devices = []
    try:
        for device_name, dashboards in devices_to_migrate.items():
            _LOGGER.info("Creating subentry for device: %s", device_name)

            # Create subentry data
            subentry_data = {
                "device_name": device_name,
                "dashboards": dashboards,
            }

            # Use the config_entries API to add a subentry
            # This is the proper way to add subentries in HA 2025+
            try:
                subentry = ConfigSubentry(
                    data=subentry_data,
                    subentry_type=SUBENTRY_TYPE_DEVICE,
                    title=device_name,
                    unique_id=device_name,
                )
                result = hass.config_entries.async_add_subentry(entry, subentry)
                if result:
                    migrated_devices.append(device_name)
                    _LOGGER.info("Successfully created subentry for %s", device_name)
                else:
                    _LOGGER.error("Failed to create subentry for %s (returned False)", device_name)
            except Exception as sub_error:
                _LOGGER.error("Failed to create subentry for %s: %s", device_name, sub_error, exc_info=True)

        if migrated_devices:
            _LOGGER.info("Successfully migrated %d devices: %s", len(migrated_devices), migrated_devices)

            # Remove legacy devices from options/data now that migration is complete
            new_options = dict(entry.options)
            new_data = dict(entry.data)

            if legacy_source == "options":
                new_options.pop("devices", None)
            elif legacy_source == "data":
                new_data.pop("devices", None)

            hass.config_entries.async_update_entry(entry, options=new_options, data=new_data)

            # Show notification
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Device Migration Complete",
                    "message": (
                        f"Successfully migrated {len(migrated_devices)} device(s) to the new format:\n"
                        f"{', '.join(migrated_devices)}\n\n"
                        "Each device now has its own Configure button on the integration page."
                    ),
                    "notification_id": f"{DOMAIN}_migration_complete",
                },
            )

            return True

    except Exception as e:
        _LOGGER.error("Error during migration: %s", str(e), exc_info=True)

    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Continuously Casting Dashboards from a config entry."""
    # Get or create a lock for this specific entry
    if entry.entry_id not in _SETUP_LOCKS:
        _SETUP_LOCKS[entry.entry_id] = asyncio.Lock()

    async with _SETUP_LOCKS[entry.entry_id]:
        _LOGGER.debug("=== SETUP ENTRY START (LOCKED): %s (ID: %s) ===", entry.title, entry.entry_id)

        try:
            # Check if this entry is already set up
            if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
                existing_data = hass.data[DOMAIN][entry.entry_id]
                _LOGGER.debug("Entry %s already exists in hass.data: %s", entry.entry_id, existing_data)
                # If platforms are already set up, we really shouldn't continue
                if existing_data.get("platforms_setup", False):
                    _LOGGER.debug("Platforms already set up for entry %s, aborting setup", entry.entry_id)
                    return True
                else:
                    _LOGGER.debug("Entry exists but platforms not set up, cleaning up first")
                    # Clean up the incomplete setup
                    if "caster" in existing_data:
                        await existing_data["caster"].stop()
                    del hass.data[DOMAIN][entry.entry_id]

            # MIGRATE LEGACY DEVICES TO SUBENTRIES
            migration_performed = await _migrate_legacy_devices_to_subentries(hass, entry)

            # If migration was performed, return early (reload will happen automatically)
            if migration_performed:
                return True

            # Register update listener
            entry.async_on_unload(entry.add_update_listener(async_reload_entry))
            entry.async_on_unload(entry.add_update_listener(async_migrate_entry))

            # Merge data from config entry with options
            config = dict(entry.data)
            config.update(entry.options)

            # Build devices from subentries (with legacy fallback)
            devices = _build_devices_from_subentries(entry)
            config["devices"] = devices
            _LOGGER.debug("Merged config: %s", config)

            # Extract configuration with fallback to defaults
            logging_level = config.get("logging_level", DEFAULT_LOGGING_LEVEL)
            cast_delay = config.get("cast_delay", DEFAULT_CAST_DELAY)
            start_time = config.get("start_time", DEFAULT_START_TIME)
            end_time = config.get("end_time", DEFAULT_END_TIME)

            # Getting new parameters using constants from const.py
            scan_interval = int(config.get("scan_interval", DEFAULT_SCAN_INTERVAL))
            max_retries = int(config.get("max_retries", DEFAULT_MAX_RETRIES))
            casting_timeout = float(config.get(CONF_CASTING_TIMEOUT, DEFAULT_CASTING_TIMEOUT))
            # Getting retry_delay from config (cast to int)
            retry_delay = int(config.get(CONF_RETRY_DELAY, DEFAULT_RETRY_DELAY))

            # Ensure directory exists
            os.makedirs("/config/google_cast_fuchsia", exist_ok=True)

            # Set up logging based on config
            log_level = logging_level.upper()
            logging.getLogger(__name__).setLevel(getattr(logging, log_level))

            # Overriding scan_interval in the config dictionary so that other managers can see it
            config["scan_interval"] = scan_interval
            config["max_retries"] = max_retries
            config[CONF_CASTING_TIMEOUT] = casting_timeout
            config[CONF_RETRY_DELAY] = retry_delay

            # Set the scan interval from cast_delay
            config[CONF_SCAN_INTERVAL] = cast_delay

            # Initialize the Continuously Casting Dashboards instance
            _LOGGER.debug("Creating ContinuouslyCastingDashboards instance")
            caster = ContinuouslyCastingDashboards(hass, config)

            # Store the caster in domain data with entry_id to support multiple entries
            hass.data.setdefault(DOMAIN, {})
            hass.data[DOMAIN][entry.entry_id] = {"caster": caster, "config": config, "platforms_setup": False}

            # FAST CORE STARTUP - only essential services, no device discovery
            _LOGGER.debug("Starting core services...")
            try:
                # Start core services only (no device initialization)
                await asyncio.wait_for(caster.start_core(), timeout=10)  # Quick 10-second timeout
                _LOGGER.debug("Core services started successfully")
                
                # Set up platforms (including sensor platform)
                _LOGGER.debug("Setting up platforms: %s", PLATFORMS)
                for platform in PLATFORMS:
                    try:
                        await _async_forward_entry_setup(hass, entry, platform)
                        _LOGGER.debug(f"Successfully set up platform {platform}")
                    except Exception as e:
                        _LOGGER.error(f"Error setting up platform {platform}: {e}")
                        raise
                
                hass.data[DOMAIN][entry.entry_id]["platforms_setup"] = True
                _LOGGER.debug("Platforms setup completed")
                
                # START BACKGROUND DEVICE INITIALIZATION - doesn't block integration loading
                _LOGGER.info("Integration loaded successfully, starting device initialization in background...")
                hass.async_create_task(caster.start_background_initialization())
                
                _LOGGER.info("Entry %s setup completed successfully", entry.entry_id)
                return True
                
            except asyncio.TimeoutError:
                _LOGGER.error("Core initialization timed out for entry %s", entry.entry_id)
                # Clean up on timeout
                if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
                    del hass.data[DOMAIN][entry.entry_id]
                return False
                
            except Exception as e:
                _LOGGER.error("Error in async_setup_entry: %s", str(e), exc_info=True)
                # Clean up on error
                if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
                    del hass.data[DOMAIN][entry.entry_id]
                raise
                
        finally:
            _LOGGER.debug("=== SETUP ENTRY END: %s ===", entry.entry_id)

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Comprehensive entry reload mechanism."""
    _LOGGER.info(f"Reloading entry {entry.entry_id}")

    try:
        # 1. Merge current data and options
        config = dict(entry.data)
        config.update(entry.options)

        # Build devices from subentries (with legacy fallback)
        devices = _build_devices_from_subentries(entry)
        config["devices"] = devices
        _LOGGER.debug(f"Reloading with config: {config}")
        _LOGGER.debug(f"Options before reload: {entry.options}")
        _LOGGER.debug(f"Subentries: {len(entry.subentries)}")

        # 2. Stop existing integration instance and unload platforms
        if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
            current_instance = hass.data[DOMAIN][entry.entry_id].get("caster")
            if current_instance:
                await current_instance.stop()
            
            # Unload all platforms first
            for platform in PLATFORMS:
                try:
                    await hass.config_entries.async_forward_entry_unload(entry, platform)
                    _LOGGER.debug(f"Unloaded platform {platform}")
                except Exception as e:
                    _LOGGER.warning(f"Error unloading platform {platform}: {e}")

            # Remove the current entry data
            del hass.data[DOMAIN][entry.entry_id]

        # 3. Create and start new instance
        new_instance = ContinuouslyCastingDashboards(hass, config)
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][entry.entry_id] = {
            "caster": new_instance, 
            "config": config,
            "platforms_setup": False  # Reset platform setup flag
        }

        # Start the new instance (core services only)
        try:
            await asyncio.wait_for(new_instance.start_core(), timeout=10)  # Fast core startup
            
            # 4. Set up platforms fresh
            _LOGGER.debug("Setting up platforms after reload")
            for platform in PLATFORMS:
                try:
                    await _async_forward_entry_setup(hass, entry, platform)
                    _LOGGER.debug(f"Successfully set up platform {platform}")
                except Exception as e:
                    _LOGGER.error(f"Error setting up platform {platform}: {e}")
                    raise
            
            hass.data[DOMAIN][entry.entry_id]["platforms_setup"] = True
            _LOGGER.info(f"Successfully reloaded integration for entry {entry.entry_id}")
            _LOGGER.debug(f"Options after reload: {entry.options}")
            
            # Start background initialization after reload
            hass.async_create_task(new_instance.start_background_initialization())
            
        except asyncio.TimeoutError:
            _LOGGER.error(f"Reload timed out for entry {entry.entry_id}")
            if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
                del hass.data[DOMAIN][entry.entry_id]
            raise
        except Exception as e:
            _LOGGER.error(f"Error during reload: {str(e)}", exc_info=True)
            if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
                del hass.data[DOMAIN][entry.entry_id]
            raise
    except Exception as ex:
        _LOGGER.error(f"Reload failed: {ex}")
        _LOGGER.exception("Detailed reload failure traceback:")
        raise
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading entry %s", entry.entry_id)

    try:
        # Unload all platforms first
        for platform in PLATFORMS:
            try:
                await hass.config_entries.async_forward_entry_unload(entry, platform)
                _LOGGER.debug(f"Unloaded platform {platform}")
            except Exception as e:
                _LOGGER.warning(f"Error unloading platform {platform}: {e}")

        # Stop the existing caster if it exists
        if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
            caster = hass.data[DOMAIN][entry.entry_id]["caster"]
            await caster.stop()

            # Remove the entry from domain data
            del hass.data[DOMAIN][entry.entry_id]

        # Clean up the setup lock for this entry
        if entry.entry_id in _SETUP_LOCKS:
            del _SETUP_LOCKS[entry.entry_id]

        return True
    except Exception as ex:
        _LOGGER.error(f"Error unloading entry: {ex}")
        return False


class ContinuouslyCastingDashboards:
    """Class to handle casting dashboards to Chromecast devices."""

    def __init__(self, hass: HomeAssistant, config: dict):
        """Initialize the dashboard caster."""
        _LOGGER.debug(f"Initializing with config: {config}")
        _LOGGER.debug(f"Devices from config: {config.get('devices', {})}")
        self.hass = hass
        self.config = config
        self.running = True
        self.started = False  # Add flag to prevent multiple starts
        self.core_started = False  # Add flag for core services
        self.background_started = False  # Add flag for background initialization
        self.unsubscribe_listeners = []

        # Initialize managers
        self.device_manager = DeviceManager(hass, config)
        self.time_window_checker = TimeWindowChecker(config)
        self.switch_checker = SwitchEntityChecker(hass, config)
        self.casting_manager = CastingManager(hass, config, self.device_manager)
        self.monitoring_manager = MonitoringManager(
            hass,
            config,
            self.device_manager,
            self.casting_manager,
            self.time_window_checker,
            self.switch_checker,
        )
        self.stats_manager = StatsManager(hass, config)

        # Share components between managers
        self.monitoring_manager.set_stats_manager(self.stats_manager)

    async def start_core(self):
        """Start core services only - fast startup for integration loading."""
        if self.core_started:
            _LOGGER.warning("Core services already started, skipping duplicate start")
            return True
            
        _LOGGER.info("Starting core services - Instance ID: %s", id(self))
        self.core_started = True

        try:
            # Set up recurring monitoring (but don't initialize devices yet)
            _LOGGER.debug("Setting up recurring monitoring - Scan interval: %s seconds", self.config.get(CONF_SCAN_INTERVAL, 30))
            scan_interval = self.config.get(CONF_SCAN_INTERVAL, 30)
            recurring_listener = async_track_time_interval(
                self.hass,
                self.monitoring_manager.async_monitor_devices,
                timedelta(seconds=scan_interval),
            )
            self.unsubscribe_listeners.append(recurring_listener)
            _LOGGER.debug("Recurring monitoring listener created: %s", id(recurring_listener))

            # Schedule regular status updates
            _LOGGER.debug("Setting up regular status updates")
            status_listener = async_track_time_interval(
                self.hass,
                self.stats_manager.async_generate_status_data,
                timedelta(minutes=1),
            )
            self.unsubscribe_listeners.append(status_listener)
            _LOGGER.debug("Status update listener created: %s", id(status_listener))

            # Mark core services as complete
            _LOGGER.info("Core services started successfully - Total listeners: %s", len(self.unsubscribe_listeners))
            return True
        except Exception as e:
            _LOGGER.error("Error in start_core(): %s", str(e), exc_info=True)
            raise

    async def start_background_initialization(self):
        """Start background device initialization - doesn't block integration loading."""
        if self.background_started:
            _LOGGER.warning("Background initialization already started, skipping duplicate start")
            return True
            
        _LOGGER.info("Starting background device initialization")
        self.background_started = True

        try:
            # Initial setup of devices - this can take time but doesn't block integration
            _LOGGER.debug("Starting device initialization in background")
            try:
                await asyncio.wait_for(
                    self.monitoring_manager.initialize_devices(),
                    timeout=90,  # 90 second timeout for initial device setup
                )
                _LOGGER.info("Background device initialization completed successfully")
            except asyncio.TimeoutError:
                _LOGGER.warning("Background device initialization timed out, continuing anyway")
            except Exception as e:
                _LOGGER.error("Error during background device initialization: %s", str(e), exc_info=True)
                # Don't raise - let the integration continue working

            # Generate initial status
            _LOGGER.debug("Generating initial status in background")
            try:
                await self.stats_manager.async_generate_status_data()
                _LOGGER.debug("Initial status generation completed")
            except Exception as e:
                _LOGGER.error("Error generating initial status: %s", str(e), exc_info=True)
                # Don't raise - let the integration continue working

            # Trigger an immediate monitoring run
            _LOGGER.debug("Triggering immediate monitoring run in background")
            monitoring_task = self.hass.async_create_task(self.monitoring_manager.async_monitor_devices(None))
            _LOGGER.debug("Immediate monitoring task created: %s", id(monitoring_task))

            # Mark background initialization as complete
            _LOGGER.info("Background initialization complete - integration fully operational")
            return True
        except Exception as e:
            _LOGGER.error("Error in background initialization: %s", str(e), exc_info=True)
            # Don't raise - let the integration continue working even if background init fails

    async def start(self):
        """Legacy start method for backward compatibility - now calls start_core."""
        return await self.start_core()

    async def stop(self):
        """Stop the casting process."""
        _LOGGER.info("Stopping Continuously Casting Dashboards integration")
        self.running = False
        self.started = False
        self.core_started = False
        self.background_started = False

        # Unsubscribe from all listeners
        for unsubscribe in self.unsubscribe_listeners:
            unsubscribe()
        
        # Clear the listeners list
        self.unsubscribe_listeners.clear()

        return True
