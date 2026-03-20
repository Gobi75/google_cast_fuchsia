"""Monitoring functionality for Continuously Casting Dashboards."""
import asyncio
import logging
import time
from datetime import datetime
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import CONF_DEVICES
from homeassistant.helpers.event import async_track_state_change_event
from .const import (
    EVENT_CONNECTION_ATTEMPT, 
    EVENT_CONNECTION_SUCCESS, 
    EVENT_RECONNECT_ATTEMPT, 
    EVENT_RECONNECT_SUCCESS, 
    EVENT_RECONNECT_FAILED,
    STATUS_CASTING_IN_PROGRESS,
    STATUS_ASSISTANT_ACTIVE,
    CONF_SWITCH_ENTITY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_DELAY,
    DEFAULT_CASTING_TIMEOUT,
    STATUS_STOPPED_BY_TIMER
)

_LOGGER = logging.getLogger(__name__)

class MonitoringManager:
    """Class to handle device monitoring and reconnection."""

    def __init__(self, hass: HomeAssistant, config: dict, device_manager, casting_manager, 
                 time_window_checker, switch_checker):
        """Initialize the monitoring manager."""
        _LOGGER.critical("MONITORING INIT CONFIG: %s", config) 
        self.hass = hass
        self.config = config
        self.device_manager = device_manager
        self.casting_manager = casting_manager
        self.time_window_checker = time_window_checker
        self.switch_checker = switch_checker
        self.stats_manager = None  # Will be set later
        self.devices = config.get(CONF_DEVICES, {})
        self.cast_delay = config.get('cast_delay', 0)
        # New parameters taken from integration configuration
        self.scan_interval = config.get('scan_interval', DEFAULT_SCAN_INTERVAL)
        self.max_retries = config.get('max_retries', DEFAULT_MAX_RETRIES)
        self.retry_delay = int(config.get('retry_delay', DEFAULT_RETRY_DELAY))
        self.casting_timeout = float(config.get('casting_timeout', DEFAULT_CASTING_TIMEOUT))
        self.active_device_configs = {}  # Track which dashboard config is active for each device
        self.monitor_lock = asyncio.Lock()  # Lock to prevent monitoring cycle overlap
        
        # Set up switch entity state change listener if configured
        self.switch_entity_id = config.get(CONF_SWITCH_ENTITY)
        if self.switch_entity_id:
            self.setup_switch_entity_listener()
    
    def setup_switch_entity_listener(self):
        """Set up a listener for the global switch entity state changes."""
        @callback
        async def switch_state_listener(event):
            """Handle the state change event for global switch entity."""
            new_state = event.data.get('new_state')
            if new_state is None:
                return
            
            if new_state.state.lower() not in ('on', 'true', 'home', 'open'):
                _LOGGER.info(f"Global switch entity {self.switch_entity_id} turned off, stopping dashboards for devices without specific switches")
                
                # Only stop dashboards for devices without their own switch
                for device_name, device_configs in self.devices.items():
                    current_config, _ = self.time_window_checker.get_current_device_config(device_name, device_configs)
                    if not current_config.get('switch_entity_id'):
                        # This device uses the global switch, stop its dashboard
                        ip = await self.device_manager.async_get_device_ip(device_name)
                        if ip:
                            full_status = await self.device_manager.async_get_full_device_status(ip)
                            is_casting = full_status.get('is_our_dashboard', False)
                            if is_casting:
                                _LOGGER.info(f"Stopping dashboard for {device_name} due to global switch off")
                                await self.async_stop_casting(ip)
                                
                                device_key = f"{device_name}_{ip}"
                                self.device_manager.update_active_device(
                                    device_key=device_key,
                                    status='stopped',
                                    last_checked=datetime.now().isoformat()
                                )
        
        # Register the listener for the global switch
        if self.switch_entity_id:
            async_track_state_change_event(
                self.hass, self.switch_entity_id, switch_state_listener
            )
            _LOGGER.info(f"Registered state change listener for global switch entity: {self.switch_entity_id}")
        
        # Set up listeners for device-specific switches
        for device_name, device_configs in self.devices.items():
            for config in device_configs:
                if 'switch_entity_id' in config:
                    device_switch = config.get('switch_entity_id')
                    if device_switch:
                        # Use a closure to capture the current device_name and config
                        @callback
                        async def device_switch_listener(event, device=device_name, conf=config):
                            """Handle the state change event for device-specific switch entity."""
                            new_state = event.data.get('new_state')
                            if new_state is None:
                                return
                            
                            entity_id = event.data.get('entity_id')
                            
                            # Check if the device is active and should be stopped
                            if new_state.state.lower() not in ('on', 'true', 'home', 'open'):
                                # Find the device IP
                                ip = await self.device_manager.async_get_device_ip(device)
                                if ip:
                                    # Check if it's currently casting our dashboard
                                    full_status = await self.device_manager.async_get_full_device_status(ip)
                                    is_casting = full_status.get('is_our_dashboard', False)
                                    if is_casting:
                                        _LOGGER.info(f"Device switch entity {entity_id} turned off for {device}, stopping dashboard")
                                        await self.async_stop_casting(ip)
                                        
                                        # Update device status
                                        device_key = f"{device}_{ip}"
                                        self.device_manager.update_active_device(
                                            device_key=device_key,
                                            status='stopped',
                                            last_checked=datetime.now().isoformat()
                                        )
                            else:
                                # If switch turned on, trigger a re-check of ONLY this specific device
                                _LOGGER.info(f"Device switch entity {entity_id} turned on for {device}, scheduling check for {device} only")
                                self.hass.async_create_task(
                                    self._async_check_single_device(
                                        device, preferred_entity_id=entity_id
                                    )
                                )
                        
                        # Register the listener for this device's switch
                        async_track_state_change_event(
                            self.hass, device_switch, device_switch_listener
                        )
                        _LOGGER.info(f"Registered state change listener for device {device_name} switch entity: {device_switch}")

    async def _process_single_device(
        self,
        device_name,
        ip,
        current_config,
        force_check=False,
        preferred_entity_id=None,
    ):
            """Process a single device - extracted from async_monitor_devices for reuse."""
            device_key = f"{device_name}_{ip}"
            
            # --- START: DOWNLOADING STATUS ---
            # Define these first so they are available everywhere below
            full_status = await self.device_manager.async_get_full_device_status(ip)
            
            is_casting = full_status.get('is_our_dashboard', False)
            is_media_playing = full_status.get('is_media_playing', False)
            assistant_active = full_status.get('is_assistant_active', False)
            is_idle = full_status.get('is_backdrop', False) or not full_status.get('is_online', False) or full_status.get('app_id') is None or full_status.get('app_id') == 'None'
            status_output = full_status.get('output', "")
            
            _LOGGER.debug(f"CYCLE_START [{device_name}]: AppID: {full_status.get('app_id')} | Our Dashboard: {is_casting}")
            # --- END: DOWNLOADING STATUS ---
            
            # --- NEW: ENTITY & WINDOW LOGIC WITH DIRECT RELOAD ---
            is_in_window = False
            device_configs = self.devices.get(device_name, [])
            
            active_device_data = self.device_manager.get_active_device(device_key)
            current_status = active_device_data.get('status', 'unknown') if active_device_data else 'unknown'
            # Get the button that was active in the PREVIOUS cycle
            last_active_button = active_device_data.get('active_switch_id') if active_device_data else None

            # 1. PRIORITY CHECK: Buttons
            selected_config = None
            selected_entity_id = None

            # Absolute priority for the button that triggered this check (if still ON).
            if preferred_entity_id:
                for config in device_configs:
                    entity_id = config.get("switch_entity_id")
                    if entity_id != preferred_entity_id:
                        continue
                    state = self.hass.states.get(entity_id)
                    if state and state.state == "on":
                        selected_config = config
                        selected_entity_id = entity_id
                        _LOGGER.info(
                            "Forced priority for %s via triggering button %s",
                            device_name,
                            entity_id,
                        )
                        break

            # Fallback: first active button from config order.
            if not selected_config:
                for config in device_configs:
                    entity_id = config.get('switch_entity_id')
                    if entity_id:
                        state = self.hass.states.get(entity_id)
                        if state and state.state == 'on':
                            selected_config = config
                            selected_entity_id = entity_id
                            _LOGGER.debug(f"!!! PRIORITY MATCH !!! Button {entity_id} is ON.")
                            break

            if selected_config and selected_entity_id:
                current_config = selected_config
                is_in_window = True

                # RELOAD TRIGGER:
                # 1. We are already casting
                # 2. AND (button changed OR we just switched from timer to button)
                if is_casting and last_active_button != selected_entity_id:
                    _LOGGER.info(
                        f"Priority change detected: {last_active_button} -> {selected_entity_id}. Reloading NOW."
                    )
                    await self.async_stop_casting(ip)
                    await asyncio.sleep(self.retry_delay)
                    await self.async_start_device(
                        device_name, current_config, ip, is_media_playing=is_media_playing
                    )
                    self.device_manager.update_active_device(
                        device_key, status='connected', active_switch_id=selected_entity_id
                    )
                    return  # Exit cycle immediately

                self.device_manager.update_active_device(
                    device_key, status=current_status, active_switch_id=selected_entity_id
                )
            
            # 2. TIMER CHECK: Only if no button is active
            if not is_in_window:
                best_config, in_time_window = self.time_window_checker.get_current_device_config(device_name, device_configs)
                if in_time_window:
                    current_config = best_config
                    is_in_window = True
                    # If we were on a button before, but now only timer, clear the button ID
                    if last_active_button:
                        _LOGGER.info(f"Button deactivated, returning to timer for {device_name}")
                        self.device_manager.update_active_device(device_key, status=current_status, active_switch_id=None)
                        # Optional: force reload to timer URL
                        await self.async_stop_casting(ip)
                        return

            # 3. EXIT LOGIC
            if not is_in_window:
                if is_casting:
                    _LOGGER.info(f"Device {device_name} outside windows. Stopping cast.")
                    await self.async_stop_casting(ip)
                    self.device_manager.update_active_device(device_key=device_key, status=STATUS_STOPPED_BY_TIMER, active_switch_id=None)
                return 

            # --- END OF PRIORITY LOGIC --- 

            # --- IMMEDIATE RELOAD FOR PRIORITY BUTTON ---
            if force_check and is_casting:
                _LOGGER.info(f"Priority reload: Stopping old and starting new URL for {device_name}")
                await self.async_stop_casting(ip)
                await asyncio.sleep(self.retry_delay)
                # Double-check: the triggering button might have been switched off
                # while we were stopping/cooling down.
                if preferred_entity_id:
                    state = self.hass.states.get(preferred_entity_id)
                    if not state or state.state != "on":
                        _LOGGER.info(
                            "Skipping async_start_device for %s: preferred entity %s is no longer ON",
                            device_name,
                            preferred_entity_id,
                        )
                        self.device_manager.update_active_device(
                            device_key,
                            status="stopped",
                            active_switch_id=None,
                            last_checked=datetime.now().isoformat(),
                        )
                        return

                await self.async_start_device(
                    device_name, current_config, ip, is_media_playing=is_media_playing
                )
                return 
            # --- END OF PRIORITY FIX ---
            
            # Check if casting is already in progress for this device
            if ip in self.casting_manager.active_casting_operations:
                _LOGGER.info(f"Casting operation in progress for {device_name} ({ip}), skipping checks")
                # Update status to indicate casting is in progress
                self.device_manager.update_active_device(
                    device_key=device_key,
                    status=STATUS_CASTING_IN_PROGRESS,
                    last_checked=datetime.now().isoformat()
                )
                return
            
            # Handle device configuration changes (only for regular monitoring, not switch-triggered)
            if not force_check and device_name in self.active_device_configs:
                active_config_info = self.active_device_configs[device_name]
                instance_change = active_config_info['instance_change']
                
                # If the instance has changed, we need to force a reload
                if instance_change:
                    _LOGGER.info(f"Dashboard configuration changed for {device_name}, forcing reload")
                    
                    # If currently casting, stop it first
                    if is_casting:  # Reuse the single status check result
                        _LOGGER.info(f"Stopping current dashboard on {device_name} before switching to new one")
                        await self.async_stop_casting(ip)
                        # Small delay to ensure the stop takes effect
                        await asyncio.sleep(self.retry_delay)
                    
                    # Cast the new dashboard
                    await self.async_start_device(device_name, current_config, ip, is_media_playing=is_media_playing)
                    
                    # Reset the instance_change flag
                    self.active_device_configs[device_name]['instance_change'] = False
                    return  # Skip normal checks since we've already handled this device
            
            # Handle device within its allowed time window
            _LOGGER.debug(f"Inside casting time window for {device_name}, continuing with normal checks")
            
            # Check if the device is part of an active speaker group
            speaker_groups = current_config.get('speaker_groups')
            if speaker_groups:
                if await self.device_manager.async_check_speaker_group_state(ip, speaker_groups):
                    _LOGGER.info(f"Speaker Group playback is active for {device_name}, skipping status check")
                    active_device = self.device_manager.get_active_device(device_key)
                    if active_device:
                        if active_device.get('status') != 'speaker_group_active':
                            self.device_manager.update_active_device(
                                device_key=device_key,
                                status='speaker_group_active',
                                last_checked=datetime.now().isoformat()
                            )
                    else:
                        self.device_manager.update_active_device(
                            device_key=device_key,
                            status='speaker_group_active',
                            name=device_name,
                            ip=ip,
                            first_seen=datetime.now().isoformat(),
                            last_checked=datetime.now().isoformat(),
                            reconnect_attempts=0,
                            app_id=full_status.get('app_id'),
                            display_name=full_status.get('display_name')
                        )
                    return
            
            # Check if Google Assistant (timer/alarm/reminder) is active (REUSED FROM FULL_STATUS)
            if assistant_active:
                _LOGGER.info(f"Google Assistant activity detected on {device_name}, pausing dashboard casting")

                if is_casting:
                    _LOGGER.info(f"Stopping dashboard on {device_name} to allow Assistant UI")
                    await self.async_stop_casting(ip)

                active_device = self.device_manager.get_active_device(device_key)
                if active_device:
                    self.device_manager.update_active_device(
                        device_key=device_key,
                        status=STATUS_ASSISTANT_ACTIVE,
                        last_checked=datetime.now().isoformat()
                    )
                else:
                    self.device_manager.update_active_device(
                        device_key=device_key,
                        status=STATUS_ASSISTANT_ACTIVE,
                        name=device_name,
                        ip=ip,
                        first_seen=datetime.now().isoformat(),
                        last_checked=datetime.now().isoformat(),
                        reconnect_attempts=0,
                        app_id=full_status.get('app_id'),
                        display_name=full_status.get('display_name')
                    )
                return

            # Check if media is playing (REUSED FROM FULL_STATUS)
            if is_media_playing:
                _LOGGER.info(f"Media is currently playing on {device_name}, skipping status check")
                # Update device status to media_playing
                active_device = self.device_manager.get_active_device(device_key)
                if active_device:
                    # If device was previously connected to our dashboard, add a delay before marking as media_playing
                    if active_device.get('status') == 'connected':
                        _LOGGER.info(f"Device {device_name} was showing our dashboard but now has media - giving it time to stabilize")
                    else:
                        self.device_manager.update_active_device(device_key, 'media_playing', last_checked=datetime.now().isoformat())
                else:
                    # First time seeing this device
                    self.device_manager.update_active_device(
                        device_key=device_key,
                        status='media_playing',
                        name=device_name,
                        ip=ip,
                        first_seen=datetime.now().isoformat(),
                        last_checked=datetime.now().isoformat(),
                        reconnect_attempts=0,
                        app_id=full_status.get('app_id'),
                        display_name=full_status.get('display_name')
                    )
                return
            
            # Handle switch-triggered immediate casting
            if force_check:
                _LOGGER.info(f"Switch-triggered check for {device_name}")
                
                if is_casting:
                    # Sprawdzamy czy to zmiana przycisku
                    if last_active_button and active_device_data.get('active_switch_id') != last_active_button:
                        _LOGGER.info(f"Switching priority for {device_name} - stopping old and starting new dashboard")
                        await self.async_stop_casting(ip)
                        await asyncio.sleep(self.retry_delay)
                        await self.async_start_device(device_name, current_config, ip, is_media_playing=is_media_playing)
                        return
                    
                    _LOGGER.info(f"Device {device_name} is already casting our dashboard")
                    # Reszta Twojej logiki aktualizacji statusu
                    active_device = self.device_manager.get_active_device(device_key)
                    if active_device:
                        self.device_manager.update_active_device(
                            device_key=device_key,
                            status='connected',
                            last_checked=datetime.now().isoformat(),
                            current_dashboard=current_config.get('dashboard_url')
                        )
                    return
                    
                    _LOGGER.info(f"Device {device_name} is already casting our dashboard")
                    # Reszta Twojej logiki aktualizacji statusu...
                    active_device = self.device_manager.get_active_device(device_key)
                    if active_device:
                        self.device_manager.update_active_device(
                            device_key=device_key,
                            status='connected',
                            last_checked=datetime.now().isoformat(),
                            current_dashboard=current_config.get('dashboard_url')
                        )
                    else:
                        self.device_manager.update_active_device(
                            device_key=device_key,
                            status='connected',
                            name=device_name,
                            ip=ip,
                            first_seen=datetime.now().isoformat(),
                            last_checked=datetime.now().isoformat(),
                            reconnect_attempts=0,
                            app_id=full_status.get('app_id'),
                            display_name=full_status.get('display_name'),
                            current_dashboard=current_config.get('dashboard_url')
                        )
                    return
                
                elif is_idle:
                    _LOGGER.info(f"Switch triggered and device {device_name} is idle - starting immediate cast")
                    # Bypass stabilization period, cast immediately
                    await self.async_start_device(device_name, current_config, ip, is_media_playing=is_media_playing)
                    return
                
                else:
                    _LOGGER.info(f"Switch triggered but device {device_name} has other content - marking status")
                    # Device has other content, just update status
                    active_device = self.device_manager.get_active_device(device_key)
                    if active_device:
                        self.device_manager.update_active_device(
                            device_key=device_key,
                            status='other_content',
                            last_checked=datetime.now().isoformat()
                        )
                    else:
                        self.device_manager.update_active_device(
                            device_key=device_key,
                            status='other_content',
                            name=device_name,
                            ip=ip,
                            first_seen=datetime.now().isoformat(),
                            last_checked=datetime.now().isoformat(),
                            reconnect_attempts=0,
                            app_id=full_status.get('app_id'),
                            display_name=full_status.get('display_name')
                        )
                    return
            
            # Regular monitoring with stabilization period
            # Update device status based on consolidated check results
            active_device = self.device_manager.get_active_device(device_key)
            if active_device:
                previous_status = active_device.get('status', 'unknown')
                last_status_change = active_device.get('last_status_change', 0)
                current_time = time.time()
                
                # Determine current state and take appropriate action
                if is_casting:
                    # Device is showing our dashboard
                    if previous_status != 'connected':
                        self.device_manager.update_active_device(
                            device_key=device_key, 
                            status='connected', 
                            last_status_change=current_time,
                            current_dashboard=current_config.get('dashboard_url')
                        )
                        _LOGGER.info(f"Device {device_name} ({ip}) is now connected")
                        self.device_manager.update_active_device(
                        device_key,
                        'connected',
                        reconnect_attempts=0
                        )
                        if self.stats_manager:
                            await self.stats_manager.async_update_health_stats(device_key, EVENT_RECONNECT_SUCCESS)
                    else:
                        self.device_manager.update_active_device(device_key, 'connected', last_checked=datetime.now().isoformat())
                elif is_idle:
                    # We RESET THE COUNTER if we see a Backdrop - it's not an error!
                    is_backdrop = "E8C28D3C" in status_output or "backdrop" in status_output.lower()
                    if is_backdrop:
                        self.device_manager.update_active_device(device_key, previous_status, reconnect_attempts=0)
                    
                    # DYNAMIC TIME: 2s for Backdrop, 30s for other idle states
                    min_time_between_reconnects = 2 if is_backdrop else 30
                    time_since_last_change = current_time - last_status_change
                    
                    if previous_status != 'disconnected':
                        if is_backdrop:
                            _LOGGER.info(f"Device {device_name} ({ip}) is Backdrop - reconnecting immediately")
                            await self.async_reconnect_device(device_name, ip, current_config, full_status=full_status)
                            return
                        
                        _LOGGER.info(f"Device {device_name} ({ip}) is idle and not casting")
                        self.device_manager.update_active_device(
                            device_key=device_key, 
                            status='disconnected', 
                            last_status_change=current_time,
                            last_checked=datetime.now().isoformat(),
                            app_id=full_status.get('app_id'),
                            display_name=full_status.get('display_name')
                        )
                    else:
                        if time_since_last_change > min_time_between_reconnects:
                            _LOGGER.info(f"Device {device_name} ({ip}) reconnecting (delay: {min_time_between_reconnects}s)")
                            await self.async_reconnect_device(device_name, ip, current_config)
                        else:
                            _LOGGER.debug(f"Device {device_name} ({ip}) waiting {int(min_time_between_reconnects - time_since_last_change)}s")
                            self.device_manager.update_active_device(device_key, 'disconnected', last_checked=datetime.now().isoformat())
                else:
                    # Device has other content
                    if previous_status != 'other_content':
                        self.device_manager.update_active_device(
                            device_key=device_key, 
                            status='other_content', 
                            last_status_change=current_time,
                            last_checked=datetime.now().isoformat(),
                            app_id=full_status.get('app_id'),
                            display_name=full_status.get('display_name')
                        )
                    else:
                        self.device_manager.update_active_device(device_key, 'other_content', last_checked=datetime.now().isoformat())
                    _LOGGER.info(f"Device {device_name} ({ip}) has other content (not our dashboard and not idle)")
            else:
                # First time seeing this device
                if is_casting:
                    status = 'connected'
                    _LOGGER.info(f"Device {device_name} ({ip}) is casting our dashboard")
                elif is_idle:
                    status = 'disconnected'
                    _LOGGER.info(f"Device {device_name} ({ip}) is idle, will attempt to connect after stabilization period")
                else:
                    status = 'other_content'
                    _LOGGER.info(f"Device {device_name} ({ip}) has other content, will not connect")
                
                self.device_manager.update_active_device(
                    device_key=device_key,
                    status=status,
                    name=device_name,
                    ip=ip,
                    first_seen=datetime.now().isoformat(),
                    last_checked=datetime.now().isoformat(),
                    last_status_change=time.time(),
                    reconnect_attempts=0,
                    app_id=full_status.get('app_id'),
                    display_name=full_status.get('display_name'),
                    current_dashboard=current_config.get('dashboard_url') if status == 'connected' else None
                )

    async def async_stop_all_dashboards(self):
        """Stop casting dashboards on all active devices."""
        _LOGGER.info("Stopping all active dashboard casts")
        
        # Get all active devices
        active_devices = self.device_manager.get_all_active_devices()
        
        # Find all devices that are currently connected (showing dashboard)
        connected_devices = {key: device for key, device in active_devices.items() 
                            if device.get('status') == 'connected'}
        
        if not connected_devices:
            _LOGGER.info("No active dashboard casts found to stop")
            return
        
        _LOGGER.info(f"Found {len(connected_devices)} active dashboard casts to stop")
        
        # Stop each connected device
        for device_key, device_info in connected_devices.items():
            ip = device_info.get('ip')
            name = device_info.get('name', 'Unknown device')
            
            if not ip:
                _LOGGER.warning(f"No IP found for device {name}, skipping stop command")
                continue
                
            _LOGGER.info(f"Stopping dashboard cast on {name} ({ip})")
            success = await self.async_stop_casting(ip)
            
            if success:
                _LOGGER.info(f"Successfully stopped dashboard cast on {name} ({ip})")
                self.device_manager.update_active_device(
                    device_key=device_key,
                    status='stopped',
                    last_checked=datetime.now().isoformat(),
                    app_id=full_status.get('app_id'),
                    display_name=full_status.get('display_name')
                )
            else:
                _LOGGER.error(f"Failed to stop dashboard cast on {name} ({ip})")
        
        _LOGGER.info("Finished stopping all active dashboard casts")
    
    def set_stats_manager(self, stats_manager):
        """Set the stats manager reference."""
        self.stats_manager = stats_manager
        # Share the device manager with stats manager
        self.stats_manager.set_device_manager(self.device_manager)
    
    async def initialize_devices(self):
        """Initialize all configured devices."""
        # Perform a single scan to find all devices
        device_ip_map = {}
        for device_name in self.devices.keys():
            ip = await self.device_manager.async_get_device_ip(device_name)
            if ip:
                device_ip_map[device_name] = ip
            else:
                _LOGGER.error(f"Could not get IP for {device_name}, skipping initial setup for this device")
                
        # Add delay between scanning and casting to avoid overwhelming the network
        await asyncio.sleep(self.retry_delay)
        
        # Start each device with appropriate delay
        for device_name, device_configs in self.devices.items():
            if device_name not in device_ip_map:
                continue
                
            ip = device_ip_map[device_name]
            
            # Get the current device config based on the time window
            current_config, is_in_window = self.time_window_checker.get_current_device_config(device_name, device_configs)
            
            # Store the active config for this device
            self.active_device_configs[device_name] = {
                'config': current_config,
                'instance_change': False,  # No change on first run
                'last_updated': datetime.now()
            }
            
            # Check if casting is enabled for this specific device
            if not await self.switch_checker.async_check_switch_entity(device_name, current_config):
                _LOGGER.info(f"Casting disabled for device {device_name}, skipping initial cast")
                continue
            
            # Skip devices outside their time window
            if not is_in_window:
                _LOGGER.info(f"Outside all casting time windows for {device_name}, skipping initial cast")
                continue
            
            # Check if device is within casting time window
            is_in_time_window = await self.time_window_checker.async_is_within_time_window(device_name, current_config)
            
            # Skip devices outside their time window
            if not is_in_time_window:
                _LOGGER.info(f"Outside casting time window for {device_name}, skipping initial cast")
                continue
            
            # Get full device status
            full_status = await self.device_manager.async_get_full_device_status(ip)
            
            # We check if the 'is_media_playing' flag is True in this status
            if full_status.get('is_media_playing', False):
                _LOGGER.info(f"Media is currently playing on {device_name}, skipping cast")
                device_key = f"{device_name}_{ip}"
                self.device_manager.update_active_device(
                    device_key=device_key,
                    status='media_playing',
                    name=device_name,
                    ip=ip,
                    first_seen=datetime.now().isoformat(),
                    last_checked=datetime.now().isoformat(),
                    reconnect_attempts=0,
                    app_id=full_status.get('app_id'),
                    display_name=full_status.get('display_name')
                )
                continue
                
            # Check if the device is part of an active speaker group
            speaker_groups = current_config.get('speaker_groups')
            if speaker_groups:
                if await self.device_manager.async_check_speaker_group_state(ip, speaker_groups):
                    _LOGGER.info(f"Speaker Group playback is active for {device_name}, skipping initial cast")
                    device_key = f"{device_name}_{ip}"
                    self.device_manager.update_active_device(
                        device_key=device_key,
                        status='speaker_group_active',
                        name=device_name,
                        ip=ip,
                        first_seen=datetime.now().isoformat(),
                        last_checked=datetime.now().isoformat(),
                        reconnect_attempts=0,
                        app_id=full_status.get('app_id'),
                        display_name=full_status.get('display_name')
                    )
                    continue
            
            # Create task for each device
            await self.async_start_device(device_name, current_config, ip)
            
            # Apply cast delay between devices
            if self.cast_delay > 0:
                await asyncio.sleep(self.cast_delay)
        
        return True
    
    async def async_start_device(self, device_name, device_config, ip=None, is_media_playing=False):
        """Start casting to a specific device."""
        _LOGGER.info(f"Starting casting to {device_name}")
        
        # Get device IP if not provided
        if not ip:
            ip = await self.device_manager.async_get_device_ip(device_name)
            if not ip:
                _LOGGER.error(f"Could not get IP for {device_name}, skipping")
                return

        # --- DEVICE LAG PROTECTION ---
        try:
            # We wait a maximum of 5 seconds for the status. If it says "not responding," we move on.
            full_status = await asyncio.wait_for(
                self.device_manager.async_get_full_device_status(ip), 
                timeout=self.casting_timeout
            )
        except Exception as e:
            _LOGGER.warning(f"Timeout or error getting status from {ip} ({device_name}): {e}")
            # We create a safe empty status so that the rest of the code doesn't crash
            full_status = {'app_id': 'Unknown', 'display_name': 'Unknown'}
        # ----------------------------------------------

        # The rest of the code remains unchanged (uses full_status.get)
        if is_media_playing:
            _LOGGER.info(f"Media is currently playing on {device_name}, skipping cast")
            device_key = f"{device_name}_{ip}"
            self.device_manager.update_active_device(
                device_key=device_key,
                status='media_playing',
                name=device_name,
                ip=ip,
                last_checked=datetime.now().isoformat(),
                reconnect_attempts=0,
                app_id=full_status.get('app_id'),
                display_name=full_status.get('display_name')
            )
            return
        
        # Check if a cast is already in progress
        if ip in self.casting_manager.active_casting_operations:
            _LOGGER.info(f"Casting already in progress for {device_name} ({ip}), skipping")
            device_key = f"{device_name}_{ip}"
            self.device_manager.update_active_device(
                device_key=device_key,
                status=STATUS_CASTING_IN_PROGRESS,
                name=device_name,
                ip=ip,
                last_checked=datetime.now().isoformat(),
                app_id=full_status.get('app_id'),
                display_name=full_status.get('display_name')
            )
            return
        
        device_key = f"{device_name}_{ip}"
        # Update device status to indicate casting is in progress
        self.device_manager.update_active_device(
            device_key=device_key,
            status=STATUS_CASTING_IN_PROGRESS,
            name=device_name,
            ip=ip,
            last_checked=datetime.now().isoformat(),
            app_id=full_status.get('app_id'),
            display_name=full_status.get('display_name')
        )
        
        if self.stats_manager:
            await self.stats_manager.async_update_health_stats(device_key, EVENT_CONNECTION_ATTEMPT)
        
        # Cast dashboard to device
        dashboard_url = device_config.get('dashboard_url')
        success = await self.casting_manager.async_cast_dashboard(ip, dashboard_url, device_config)
        
        if success:
            _LOGGER.info(f"Successfully connected to {device_name} ({ip})")
            self.device_manager.update_active_device(
                device_key=device_key,
                status='connected',
                name=device_name,
                ip=ip,
                last_checked=datetime.now().isoformat(),
                reconnect_attempts=0,
                app_id=full_status.get('app_id'),
                display_name=full_status.get('display_name'),
                current_dashboard=dashboard_url
            )
            if self.stats_manager:
                await self.stats_manager.async_update_health_stats(device_key, EVENT_CONNECTION_SUCCESS)
        else:
            _LOGGER.error(f"Failed to connect to {device_name} ({ip})")
            self.device_manager.update_active_device(
                device_key=device_key,
                status='disconnected',
                name=device_name,
                ip=ip,
                last_checked=datetime.now().isoformat(),
                reconnect_attempts=0,
                app_id=full_status.get('app_id'),
                display_name=full_status.get('display_name')
            )
    
    async def async_update_device_configs(self):
        """Update the active device configurations based on the current time."""
        updated_devices = []
        
        for device_name, device_configs in self.devices.items():
            # Get the current device config based on the time window
            current_config, is_in_window = self.time_window_checker.get_current_device_config(device_name, device_configs)
            
            # Check if this device already has an active config
            if device_name in self.active_device_configs:
                previous_config = self.active_device_configs[device_name]['config']
                
                # Check if the dashboard URL has changed
                if (previous_config.get('dashboard_url') != current_config.get('dashboard_url')):
                    _LOGGER.info(f"Dashboard configuration changed for {device_name}: new dashboard URL: {current_config.get('dashboard_url')}")
                    self.active_device_configs[device_name] = {
                        'config': current_config,
                        'instance_change': True,
                        'last_updated': datetime.now()
                    }
                    updated_devices.append(device_name)
                else:
                    # No change, just update the timestamp
                    self.active_device_configs[device_name]['last_updated'] = datetime.now()
                    self.active_device_configs[device_name]['instance_change'] = False
            else:
                # First time seeing this device
                self.active_device_configs[device_name] = {
                    'config': current_config,
                    'instance_change': False,  # No change on first run
                    'last_updated': datetime.now()
                }
        
        return updated_devices

    async def async_monitor_devices(self, *args):
        """Monitor all devices and reconnect if needed."""
        # Use a lock to prevent monitoring cycles from overlapping
        if self.monitor_lock.locked():
            _LOGGER.debug("Previous monitoring cycle still running, skipping this cycle")
            return
            
        async with self.monitor_lock:
            _LOGGER.debug("Running device status check")
            
            # Update device configurations based on time windows
            updated_devices = await self.async_update_device_configs()
            if updated_devices:
                _LOGGER.info(f"Devices with updated dashboard configurations: {updated_devices}")
                
            # Scan for all devices at once and store IPs - with better error handling
            device_ip_map = {}
            scan_futures = []
            
            # Start all IP lookups concurrently with timeouts
            for device_name in self.devices.keys():
                future = asyncio.ensure_future(self._get_device_ip_with_timeout(device_name))
                scan_futures.append((device_name, future))
            
            # Wait for all lookups to complete
            for device_name, future in scan_futures:
                try:
                    ip = await future
                    if ip:
                        device_ip_map[device_name] = ip
                    else:
                        _LOGGER.warning(f"Could not get IP for {device_name}, skipping check")
                except Exception as e:
                    _LOGGER.error(f"Error getting IP for {device_name}: {str(e)}, skipping check")
            
            # Process each device with its known IP using the optimized single device processor
            for device_name in list(self.devices.keys()):
                # Skip if we couldn't get the IP
                if device_name not in device_ip_map:
                    continue
                    
                ip = device_ip_map[device_name]
                
                # Get the current device config
                if device_name not in self.active_device_configs:
                    _LOGGER.warning(f"No active configuration for {device_name}, skipping")
                    continue
                    
                active_config_info = self.active_device_configs[device_name]
                current_config = active_config_info['config']
                
                # Process this device using the optimized single device method
                await self._process_single_device(device_name, ip, current_config)

    async def _async_check_single_device(
        self, device_name: str, preferred_entity_id: str | None = None
    ) -> None:
        """
        Check/act on a single device.

        Used by switch state listeners so we don't wait for the next monitoring tick.
        """
        # Avoid overlapping runs with the main monitoring loop.
        if self.monitor_lock.locked():
            _LOGGER.debug(
                "Skipping single-device check for %s: monitoring cycle already running",
                device_name,
            )
            return

        device_configs = self.devices.get(device_name, [])
        current_config = None

        # Prefer cached config (keeps behavior consistent with the main loop),
        # but fall back to a time-window selection.
        if device_name in self.active_device_configs:
            current_config = self.active_device_configs[device_name].get("config")

        if not current_config:
            current_config, _ = self.time_window_checker.get_current_device_config(
                device_name, device_configs
            )

        if not current_config:
            _LOGGER.warning("No dashboard configuration found for %s", device_name)
            return

        ip = await self._get_device_ip_with_timeout(device_name)
        if not ip:
            _LOGGER.warning("Could not resolve IP for %s, skipping check", device_name)
            return

        await self._process_single_device(
            device_name,
            ip,
            current_config,
            force_check=True,
            preferred_entity_id=preferred_entity_id,
        )

    async def async_stop_casting(self, ip):
        """Stop casting on a device."""
        try:
            # Check if a cast operation is in progress
            if ip in self.casting_manager.active_casting_operations:
                _LOGGER.info(f"Casting operation in progress for {ip}, waiting for it to complete before stopping")
                # Wait up to casting_timeout for the operation to complete
                max_wait_cycles = max(1, int(self.casting_timeout / max(self.retry_delay, 1)))
                for _ in range(max_wait_cycles):
                    if ip not in self.casting_manager.active_casting_operations:
                        break
                    await asyncio.sleep(self.retry_delay)
                
                if ip in self.casting_manager.active_casting_operations:
                    _LOGGER.warning(
                        "Casting operation still in progress after %.1fs wait, proceeding with stop",
                        self.casting_timeout,
                    )
            
            cmd = ['catt', '-d', ip, 'stop']
            _LOGGER.debug(f"Executing stop command: {' '.join(cmd)}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self.casting_timeout
                )
                
                # Log the results
                stdout_str = stdout.decode().strip()
                stderr_str = stderr.decode().strip()
                _LOGGER.debug(f"Stop command stdout: {stdout_str}")
                _LOGGER.debug(f"Stop command stderr: {stderr_str}")
                
                if process.returncode == 0:
                    _LOGGER.info(f"Successfully stopped casting on device at {ip}")
                    return True
                else:
                    _LOGGER.error(f"Failed to stop casting on device at {ip}: {stderr_str}")
                    return False
            except asyncio.TimeoutError:
                _LOGGER.error(f"Stop command timed out for {ip}")
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.retry_delay)
                except asyncio.TimeoutError:
                    process.kill()
                return False
                
        except Exception as e:
            _LOGGER.error(f"Error stopping casting on device at {ip}: {str(e)}")
            return False

    async def async_reconnect_device(self, device_name, ip, device_config, full_status=None):
        """Zoptymalizowany reconnect - uzywa danych z full_status zamiast pytac urzadzenie."""
        device_key = f"{device_name}_{ip}"
        
        # If we didn't pass the status (e.g. manual call), get it
        if not full_status:
            full_status = await self.device_manager.async_get_full_device_status(ip)
        
        # We extract the necessary data from the status
        is_media_playing = full_status.get('is_media_playing', False)
        is_backdrop = full_status.get('is_backdrop', False)
        is_tts_receiver = "CC1AD845" in full_status.get('output', "")
        is_our_dash = full_status.get('is_our_dashboard', False)
        status_output = full_status.get('output', "")

        # BLOCK 1: Checking the ongoing casting
        if ip in self.casting_manager.active_casting_operations:
            _LOGGER.info(f"Casting already in progress for {device_name} ({ip}), skipping reconnect")
            self.device_manager.update_active_device(
                device_key=device_key,
                status=STATUS_CASTING_IN_PROGRESS,
                last_checked=datetime.now().isoformat(),
                app_id=full_status.get('app_id'),
                display_name=full_status.get('display_name')
            )
            return False
        
        # --- NOWA LOGIKA: Sprawdzanie czy przycisk wymusza połączenie ---
        is_forced_by_button = False
        entity_id = device_config.get('switch_entity_id')
        if entity_id:
            state = self.hass.states.get(entity_id)
            if state and state.state == 'on':
                is_forced_by_button = True
                _LOGGER.debug(f"Reconnect for {device_name} forced by button {entity_id}")

        # Jeśli przycisk NIE wymusza, sprawdzamy standardowe okno czasowe
        if not is_forced_by_button:
            if not await self.time_window_checker.async_is_within_time_window(device_name, device_config):
                _LOGGER.info(f"Outside casting time window for {device_name}, skipping reconnect")
                return False
        # --- KONIEC NOWEJ LOGIKI ---
        
        # BLOCK 3 & 4: Media and Groups (We use ready-made data)
        if is_media_playing:
            _LOGGER.info(f"Media is currently playing on {device_name}, skipping reconnect")
            self.device_manager.update_active_device(device_key, 'media_playing')
            return False

        # BLOCK 5: Counter Management and Backing Off
        active_device = self.device_manager.get_active_device(device_key)
        if active_device:
            attempts = active_device.get('reconnect_attempts', 0) + 1
            
            # Resetting the counter if we see Backdrop/TTS/Our
            if is_backdrop or is_tts_receiver or is_our_dash:
                attempts = 0
            
            self.device_manager.update_active_device(device_key, active_device.get('status'), reconnect_attempts=attempts)
            
            if attempts > self.max_retries:
                _LOGGER.warning(f"Device {device_name} ({ip}) has had {attempts} reconnect attempts, backing off")
                if self.stats_manager:
                    await self.stats_manager.async_update_health_stats(device_key, EVENT_RECONNECT_FAILED)
                return False

        # BLOCK 6: Recast Security Decision (We use data from full_status)
        app_id = full_status.get('app_id')
        is_none = app_id is None or app_id == 'None' or app_id == ''
        is_safe_to_recast = is_backdrop or is_our_dash or is_none or "8123" in status_output
        if not is_safe_to_recast and len(status_output.splitlines()) > 5:
            _LOGGER.info(f"Device {device_name} ({ip}) shows non-idle status (other content), skipping reconnect")
            self.device_manager.update_active_device(device_key, 'other_content')
            return False
        
        # BLOCK 7: Casting
        self.device_manager.update_active_device(
            device_key=device_key,
            status=STATUS_CASTING_IN_PROGRESS,
            last_checked=datetime.now().isoformat()
        )
        
        _LOGGER.info(f"Attempting to reconnect to {device_name} ({ip})")
        if self.stats_manager:
            await self.stats_manager.async_update_health_stats(device_key, EVENT_RECONNECT_ATTEMPT)
        
        dashboard_url = device_config.get('dashboard_url')
        success = await self.casting_manager.async_cast_dashboard(ip, dashboard_url, device_config)
        
        if success:
            _LOGGER.info(f"Successfully reconnected to {device_name} ({ip})")
            self.device_manager.update_active_device(
                device_key=device_key,
                status='connected',
                reconnect_attempts=0,
                app_id=full_status.get('app_id'),
                display_name=full_status.get('display_name'),
                last_reconnect=datetime.now().isoformat(),
                current_dashboard=dashboard_url
            )
            if self.stats_manager:
                await self.stats_manager.async_update_health_stats(device_key, EVENT_RECONNECT_SUCCESS)
            return True
        else:
            _LOGGER.error(f"Failed to reconnect to {device_name} ({ip})")
            self.device_manager.update_active_device(device_key, 'disconnected', last_checked=datetime.now().isoformat())
            if self.stats_manager:
                await self.stats_manager.async_update_health_stats(device_key, EVENT_RECONNECT_FAILED)
            return False

    async def _get_device_ip_with_timeout(self, device_name, timeout=None):
        """Get device IP with timeout to prevent hanging."""
        try:
            effective_timeout = self.casting_timeout if timeout is None else timeout
            return await asyncio.wait_for(
                self.device_manager.async_get_device_ip(device_name),
                timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            _LOGGER.error(
                "Timed out getting IP for %s after %.1f seconds",
                device_name,
                effective_timeout,
            )
            return None
        except Exception as e:
            _LOGGER.error(f"Error getting IP for {device_name}: {str(e)}")
            return None
