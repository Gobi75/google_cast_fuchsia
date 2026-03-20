"""Device discovery and management for Continuously Casting Dashboards."""
import asyncio
import logging
import time
import re
from datetime import datetime
from homeassistant.core import HomeAssistant

# We import the constant to make the statistics count consistent with the rest of the system
from .const import STATUS_STOPPED_BY_TIMER

_LOGGER = logging.getLogger(__name__)

# Simple IPv4 validation regex
IP_PATTERN = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')

class DeviceManager:
    """Class to manage device discovery and status checks."""

    def __init__(self, hass: HomeAssistant, config: dict):
        """Initialize the device manager."""
        self.hass = hass
        self.config = config
        self.device_ip_cache = {}    # Cache for device IPs
        self.active_devices = {}     # Track active devices
        self.active_checks = {}      # Track active status checks
        self.status_cache = {}       # Short-lived cache for catt status output

    def _cache_status_output(self, ip, output):
        """Cache status output briefly to avoid duplicate catt calls."""
        if not output:
            return
        self.status_cache[ip] = {
            "output": output,
            "timestamp": time.time(),
        }

    def _get_cached_status_output(self, ip, max_age=2.0):
        """Get cached status output if it's fresh enough."""
        cached = self.status_cache.get(ip)
        if not cached:
            return None
        if (time.time() - cached.get("timestamp", 0)) > max_age:
            return None
        return cached.get("output")

    def _status_indicates_assistant_activity(self, status_output):
        """Detect Google Assistant/timer activity from catt status output."""
        if not status_output:
            return False
        status_lower = status_output.lower()
        # We are ignoring mentions of Home Assistant to avoid confusion with Google Assistant.
        sanitized = status_lower.replace("homeassistant", "").replace("home assistant", "")

        if "google assistant" in sanitized or re.search(r"\bassistant\b", sanitized):
            return True

        assistant_keywords = ["timer", "alarm", "reminder", "stopwatch", "countdown"]
        return any(keyword in sanitized for keyword in assistant_keywords)

    async def _async_run_status_command(self, ip, timeout=15):
        """Run status command and show full output ONLY in DEBUG mode."""
        cmd = ['catt', '-d', ip, 'info']
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            output = stdout.decode().strip()
            
            if output:
                _LOGGER.debug(f"--- FULL DEVICE STATUS [{ip}] ---\n{output}\n---------------------------")
            
            return output, stderr.decode(), process.returncode, None
        except Exception as e:
            return None, None, None, str(e)

    async def async_get_full_device_status(self, ip):
        """STATUS GETTING: One request to Chromecast, complete feedback."""
        stdout_str, _, returncode, _ = await self._async_run_status_command(ip)
        
        if not stdout_str or returncode != 0:
            return {
                "is_online": False, 
                "is_our_dashboard": False, 
                "is_media_playing": False, 
                "is_assistant_active": False, 
                "is_backdrop": False,
                "app_id": None,
                "output": ""
            }

        status_lower = stdout_str.lower()
        
        # --- NEW: Extracting app_id from text---
        current_app_id = None
        for line in stdout_str.splitlines():
            if "app_id:" in line.lower():
                current_app_id = line.split(":")[-1].strip()
                break
        # ---------------------------------------
        
        # 1. Our Dashboard (AppID DashCast)
        is_ours = any(x in status_lower for x in ["84912283", "dashcast"])
        
        # 2. Backdrop 
        is_backdrop = any(x in status_lower for x in ["e8c28d3c", "backdrop"])
        
        # 3. Google Assistant (Timer/Alarm)
        assistant_active = self._status_indicates_assistant_activity(stdout_str)
        
        # 4. Multimedia (Spotify/YouTube itp.)
        media_playing = any(x in stdout_str for x in ["PLAYING", "PAUSED", "BUFFERING"])
        is_media = media_playing and not is_ours and not is_backdrop

        return {
            "is_online": True,
            "is_our_dashboard": is_ours,
            "is_media_playing": is_media,
            "is_assistant_active": assistant_active,
            "is_backdrop": is_backdrop,
            "app_id": current_app_id,
            "output": stdout_str
        }

    async def _async_execute_device_command(self, ip, command_str, timeout=10.0):
        """Execute a control command via catt."""
        cmd_parts = command_str.split()
        cmd = ['catt', '-d', ip] + cmd_parts
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(process.communicate(), timeout=timeout)
            return process.returncode == 0
        except Exception as e:
            _LOGGER.error(f"Failed to execute '{command_str}' on {ip}: {str(e)}")
            return False

    async def async_get_device_ip(self, device_name_or_ip):
        """Get IP address for a device name or directly use IP."""
        if IP_PATTERN.match(device_name_or_ip):
            return device_name_or_ip
        
        try:
            process = await asyncio.create_subprocess_exec(
                'catt', 'scan', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15.0)
            for line in stdout.decode().splitlines():
                if ' - ' in line:
                    parts = line.split(' - ')
                    ip, found_name = parts[0].strip(), parts[1].strip()
                    if found_name.lower() == device_name_or_ip.lower():
                        return ip
            return None
        except:
            return None

    async def async_check_speaker_group_state(self, ip, speaker_groups):
        """Check if any of the speaker groups is active."""
        if not speaker_groups: return False
        for group in speaker_groups:
            try:
                cmd = ['catt', '-d', group, 'info']
                p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
                out, _ = await asyncio.wait_for(p.communicate(), timeout=5.0)
                if "PLAYING" in out.decode(): return True
            except: continue
        return False

    def get_active_device(self, device_key):
        return self.active_devices.get(device_key)

    def update_active_device(self, device_key, status, **kwargs):
        if device_key in self.active_devices:
            self.active_devices[device_key].update(status=status, **kwargs)
        else:
            self.active_devices[device_key] = {'status': status, **kwargs}

    def get_all_active_devices(self):
        return self.active_devices

    def get_device_current_dashboard(self, device_key):
        return self.active_devices.get(device_key, {}).get('current_dashboard')

    def get_summary_stats(self):
        """Calculate summary statistics for all devices including timer stops."""
        stats = {
            "total_devices": len(self.active_devices),
            "connected_devices": 0,
            "disconnected_devices": 0,
            "media_playing_devices": 0,
            "other_content_devices": 0,
            "assistant_active_devices": 0,
            "stopped_by_timer_devices": 0,
        }

        for device in self.active_devices.values():
            status = device.get('status')
            if status == 'connected':
                stats["connected_devices"] += 1
            elif status == 'disconnected':
                stats["disconnected_devices"] += 1
            elif status == 'media_playing':
                stats["media_playing_devices"] += 1
            elif status == 'other_content':
                stats["other_content_devices"] += 1
            elif status == 'assistant_active':
                stats["assistant_active_devices"] += 1
            elif status == STATUS_STOPPED_BY_TIMER:
                stats["stopped_by_timer_devices"] += 1

        return stats
