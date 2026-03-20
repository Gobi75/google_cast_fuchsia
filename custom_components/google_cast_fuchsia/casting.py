"""Casting functionality - precise volume and clean logging."""
import asyncio
import logging
import time
import re
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

class CastingManager:
    def __init__(self, hass: HomeAssistant, config: dict, device_manager):
        self.hass = hass
        self.config = config
        self.device_manager = device_manager
        self.active_casting_operations = {}
        self.active_subprocesses = {}
        self.default_volume = int(config.get("default_volume", 5))

    async def _get_raw_info(self, ip):
        key = f"{ip}_info"
        try:
            process = await asyncio.create_subprocess_exec(
                "catt", "-d", ip, "info", stdout=asyncio.subprocess.PIPE
            )
            self.active_subprocesses[key] = process
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
            return stdout.decode().strip()
        except Exception:
            return ""
        finally:
            self.active_subprocesses.pop(key, None)

    async def async_cast_dashboard(self, ip, dashboard_url, device_config):
        if ip in self.active_casting_operations: return False
        self.active_casting_operations[ip] = {'start_time': time.time()}
        try:
            # 1. Get volume - Logic improved with checkbox priority
            if device_config.get("override_volume", False):
                vol_to_set = int(device_config.get("volume", 5))
                log_msg = f"Forced volume from settings: {vol_to_set}%"
            else:
                info = await self._get_raw_info(ip)
                match = re.search(r'volume_level:\s*([\d\.]+)', info)
                vol_raw = match.group(1) if match else "0.1"
                vol_to_set = int(round(float(vol_raw) * 100))
                log_msg = f"Volume remembered: {vol_to_set}%"

            _LOGGER.info(f"CAST START: {ip} | {log_msg}")

            # 2. Cast procedure - Double-tap strategy
            # First tap: Wake-up call to initialize Cast session
            _LOGGER.info("Waking up device at %s", ip)
            process_wake = await asyncio.create_subprocess_exec(
                "catt", "-d", ip, "cast_site", dashboard_url
            )
            self.active_subprocesses[f"{ip}_wake"] = process_wake
            await process_wake.wait()
            self.active_subprocesses.pop(f"{ip}_wake", None)

            # Wait for device to be ready for the actual URL load
            await asyncio.sleep(self.retry_delay)

            # Second tap: Actual cast command with strict timeout
            _LOGGER.info("Actual cast for %s", ip)
            process_cast = await asyncio.create_subprocess_exec(
                "catt", "-d", ip, "cast_site", dashboard_url
            )
            self.active_subprocesses[f"{ip}_cast_site"] = process_cast
            
            try:
                # Use wait_for to prevent hanging if catt freezes
                await asyncio.wait_for(process_cast.wait(), timeout=self.casting_timeout)
            except asyncio.TimeoutError:
                _LOGGER.error("Cast command timed out for %s after %ss", ip, self.casting_timeout)
                if f"{ip}_cast_site" in self.active_subprocesses:
                    p = self.active_subprocesses.pop(f"{ip}_cast_site")
                    p.terminate()
                return False
            finally:
                self.active_subprocesses.pop(f"{ip}_cast_site", None)

            # 3. Stabilization
            await asyncio.sleep(15)
            
            # 4. Setting the target volume
            process = await asyncio.create_subprocess_exec(
                "catt", "-d", ip, "volume", str(vol_to_set)
            )
            self.active_subprocesses[f"{ip}_volume_set"] = process
            await process.wait()
            self.active_subprocesses.pop(f"{ip}_volume_set", None)
            _LOGGER.info(f"CAST SUCCESS: {ip} | Dashboard active, volume: {vol_to_set}%")

            return True
        except Exception as e:
            _LOGGER.error(f"CAST ERROR on {ip}: {e}")
            return False
        finally:
            self.active_casting_operations.pop(ip, None)

    async def async_get_current_volume(self, ip):
        """Read current device volume from catt info output."""
        key = f"{ip}_volume"
        try:
            process = await asyncio.create_subprocess_exec(
                "catt",
                "-d",
                ip,
                "info",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.active_subprocesses[key] = process
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            if process.returncode != 0:
                _LOGGER.warning(
                    "Failed to read volume from %s via catt info: %s",
                    ip,
                    stderr.decode().strip(),
                )
                return self.default_volume

            info = stdout.decode(errors="ignore")

            # catt commonly reports "volume_level: 0.x"; keep support for "volume: x".
            match = re.search(
                r"(?:^|\n)\s*(?:volume|volume_level)\s*:\s*([0-9]*\.?[0-9]+)",
                info,
                re.IGNORECASE,
            )
            if not match:
                return self.default_volume

            raw_value = float(match.group(1))
            if raw_value <= 1.0:
                return int(round(raw_value * 100))
            return int(round(raw_value))
        except Exception as exc:
            _LOGGER.warning("Error reading current volume for %s: %s", ip, exc)
            return self.default_volume
        finally:
            self.active_subprocesses.pop(key, None)

    async def cleanup(self):
        """Terminate any tracked subprocesses still running."""
        for key, process in list(self.active_subprocesses.items()):
            try:
                if process and process.returncode is None:
                    process.terminate()
            except Exception as exc:
                _LOGGER.debug("Failed terminating subprocess %s: %s", key, exc)
            finally:
                self.active_subprocesses.pop(key, None)
