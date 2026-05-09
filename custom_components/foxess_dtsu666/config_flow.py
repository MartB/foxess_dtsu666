"""Config flow for FoxESS DTSU666 Sniffer."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import serial.tools.list_ports
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ALWAYS_POLL,
    CONF_BAUDRATE,
    CONF_BYTESIZE,
    CONF_CONNECTION_TYPE,
    CONF_DEVICE_NAME,
    CONF_HOST,
    CONF_PARITY,
    CONF_PORT,
    CONF_SERIAL_PORT,
    CONF_SLAVE_ID,
    CONF_STALENESS,
    CONF_STOPBITS,
    CONF_UPDATE_INTERVAL,
    CONN_SERIAL,
    CONN_TCP,
    DEFAULT_BAUDRATE,
    DEFAULT_BYTESIZE,
    DEFAULT_PARITY,
    DEFAULT_STALENESS,
    DEFAULT_STOPBITS,
    DEFAULT_TCP_PORT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .sniffer import async_discover_slaves


def _serial_ports() -> list[str]:
    """Return stable /dev/serial/by-id symlinks, falling back to enumerated device nodes."""
    by_id = Path("/dev/serial/by-id")
    if by_id.is_dir():
        paths = sorted(str(p) for p in by_id.iterdir())
        if paths:
            return paths
    return sorted(p.device for p in serial.tools.list_ports.comports() if p.device)


def _timing_fields(update_interval: int, staleness: int) -> dict:
    """Schema fields shared by both transport setup forms and the options flow."""
    return {
        vol.Optional(CONF_UPDATE_INTERVAL, default=update_interval): NumberSelector(
            NumberSelectorConfig(min=1, max=60, unit_of_measurement="s", mode=NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_STALENESS, default=staleness): NumberSelector(
            NumberSelectorConfig(min=10, max=3600, unit_of_measurement="s", mode=NumberSelectorMode.BOX)
        ),
    }


def _common_finish_data(user_input: dict) -> dict:
    """Extract the three fields that are identical in both serial and TCP finish paths."""
    return {
        CONF_UPDATE_INTERVAL: int(user_input.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
        CONF_STALENESS: int(user_input.get(CONF_STALENESS, DEFAULT_STALENESS)),
        CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME, ""),
    }


class Dtsu666ConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._conn_type: str | None = None
        self._reconfigure: bool = False
        self._defaults: dict = {}
        self._finish_data: dict = {}
        self._discovered_slaves: list[int] = []
        self._discovery_task = None

    # ------------------------------------------------------------------
    # Initial setup
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_connection_type(user_input)

    # ------------------------------------------------------------------
    # Reconfigure (edit an existing entry)
    # ------------------------------------------------------------------

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._reconfigure = True
        self._defaults = dict(self._get_reconfigure_entry().data)
        return await self.async_step_connection_type(user_input)

    # ------------------------------------------------------------------
    # Shared steps
    # ------------------------------------------------------------------

    async def async_step_connection_type(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._conn_type = user_input[CONF_CONNECTION_TYPE]
            if self._conn_type == CONN_SERIAL:
                return await self.async_step_serial()
            return await self.async_step_tcp()

        return self.async_show_form(
            step_id="connection_type",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONNECTION_TYPE,
                        default=self._defaults.get(CONF_CONNECTION_TYPE, CONN_SERIAL),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[CONN_SERIAL, CONN_TCP],
                            mode=SelectSelectorMode.LIST,
                            translation_key="connection_type",
                        )
                    )
                }
            ),
        )

    async def async_step_serial(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        d = self._defaults

        if user_input is not None:
            port = user_input.get(CONF_SERIAL_PORT, "").strip()
            if not port:
                errors[CONF_SERIAL_PORT] = "invalid_serial_port"
            else:
                data = {
                    CONF_CONNECTION_TYPE: CONN_SERIAL,
                    CONF_SERIAL_PORT: port,
                    CONF_BAUDRATE: int(user_input.get(CONF_BAUDRATE, DEFAULT_BAUDRATE)),
                    CONF_PARITY: user_input.get(CONF_PARITY, DEFAULT_PARITY),
                    CONF_BYTESIZE: int(user_input.get(CONF_BYTESIZE, DEFAULT_BYTESIZE)),
                    CONF_STOPBITS: int(user_input.get(CONF_STOPBITS, DEFAULT_STOPBITS)),
                    **_common_finish_data(user_input),
                }
                self._finish_data = data
                return await self.async_step_discover()

        ports = await self.hass.async_add_executor_job(_serial_ports)
        current_port = d.get(CONF_SERIAL_PORT, "")
        # Ensure the existing port appears in the list even if it's no longer detected
        if current_port and current_port not in ports:
            ports = [current_port, *ports]

        return self.async_show_form(
            step_id="serial",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERIAL_PORT, default=current_port or vol.UNDEFINED): (
                        SelectSelector(SelectSelectorConfig(options=ports, custom_value=True, mode=SelectSelectorMode.DROPDOWN))
                        if ports
                        else TextSelector()
                    ),
                    vol.Optional(CONF_BAUDRATE, default=int(d.get(CONF_BAUDRATE, DEFAULT_BAUDRATE))): NumberSelector(
                        NumberSelectorConfig(min=1200, max=115200, mode=NumberSelectorMode.BOX)
                    ),
                    vol.Optional(CONF_PARITY, default=d.get(CONF_PARITY, DEFAULT_PARITY)): SelectSelector(
                        SelectSelectorConfig(options=["N", "E", "O"], mode=SelectSelectorMode.DROPDOWN, translation_key="parity")
                    ),
                    **_timing_fields(
                        int(d.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
                        int(d.get(CONF_STALENESS, DEFAULT_STALENESS)),
                    ),
                    vol.Optional(CONF_DEVICE_NAME, default=d.get(CONF_DEVICE_NAME, "")): TextSelector(),
                }
            ),
        )

    async def async_step_tcp(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        d = self._defaults

        if user_input is not None:
            host = user_input.get(CONF_HOST, "").strip()
            if not host:
                errors[CONF_HOST] = "invalid_host"
            else:
                try:
                    await asyncio.wait_for(
                        asyncio.open_connection(host, int(user_input.get(CONF_PORT, DEFAULT_TCP_PORT))),
                        timeout=5,
                    )
                except (OSError, asyncio.TimeoutError):
                    errors["base"] = "cannot_connect"
                else:
                    data = {
                        CONF_CONNECTION_TYPE: CONN_TCP,
                        CONF_HOST: host,
                        CONF_PORT: int(user_input.get(CONF_PORT, DEFAULT_TCP_PORT)),
                        **_common_finish_data(user_input),
                    }
                    self._finish_data = data
                    return await self.async_step_discover()

        return self.async_show_form(
            step_id="tcp",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=d.get(CONF_HOST, vol.UNDEFINED)): TextSelector(),
                    vol.Optional(CONF_PORT, default=int(d.get(CONF_PORT, DEFAULT_TCP_PORT))): NumberSelector(
                        NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
                    ),
                    **_timing_fields(
                        int(d.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
                        int(d.get(CONF_STALENESS, DEFAULT_STALENESS)),
                    ),
                    vol.Optional(CONF_DEVICE_NAME, default=d.get(CONF_DEVICE_NAME, "")): TextSelector(),
                }
            ),
        )

    async def async_step_discover(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if self._discovery_task is None:
            d = self._finish_data
            if d.get(CONF_CONNECTION_TYPE) == CONN_SERIAL:
                kwargs: dict = dict(
                    serial_port=d[CONF_SERIAL_PORT],
                    baudrate=int(d.get(CONF_BAUDRATE, DEFAULT_BAUDRATE)),
                    parity=d.get(CONF_PARITY, DEFAULT_PARITY),
                    bytesize=int(d.get(CONF_BYTESIZE, DEFAULT_BYTESIZE)),
                    stopbits=int(d.get(CONF_STOPBITS, DEFAULT_STOPBITS)),
                )
            else:
                kwargs = dict(host=d[CONF_HOST], port=int(d.get(CONF_PORT, DEFAULT_TCP_PORT)))
            self._discovery_task = self.hass.async_create_task(
                async_discover_slaves(**kwargs, timeout=10.0)
            )

        if not self._discovery_task.done():
            return self.async_show_progress(
                step_id="discover",
                progress_action="discovering",
                progress_task=self._discovery_task,
            )

        try:
            self._discovered_slaves = self._discovery_task.result()
        except Exception:
            self._discovered_slaves = []

        return self.async_show_progress_done(next_step_id="select_slave")

    async def async_step_select_slave(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            slave_id = int(float(user_input.get(CONF_SLAVE_ID, 0) or 0))
            always_poll = bool(user_input.get(CONF_ALWAYS_POLL, False))
            return await self._finish({**self._finish_data, CONF_SLAVE_ID: slave_id, CONF_ALWAYS_POLL: always_poll})

        discovered = self._discovered_slaves
        options = [{"value": str(s), "label": f"Slave {s} (0x{s:02X})"} for s in discovered]
        options.append({"value": "0", "label": "Disable active polling"})
        existing = str(self._defaults.get(CONF_SLAVE_ID, 0))
        if existing != "0" and existing not in {o["value"] for o in options}:
            options.insert(0, {"value": existing, "label": f"Keep current — slave {existing}"})
        default = existing if existing != "0" else (str(discovered[0]) if discovered else "0")
        slaves_text = ", ".join(f"Slave {s} (0x{s:02X})" for s in discovered) if discovered else "none"

        return self.async_show_form(
            step_id="select_slave",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SLAVE_ID, default=default): SelectSelector(
                        SelectSelectorConfig(options=options, custom_value=True, mode=SelectSelectorMode.LIST)
                    ),
                    vol.Optional(CONF_ALWAYS_POLL, default=bool(self._defaults.get(CONF_ALWAYS_POLL, False))): BooleanSelector(),
                }
            ),
            description_placeholders={"count": str(len(discovered)), "slaves": slaves_text},
        )

    async def _finish(self, data: dict) -> FlowResult:
        name = data.get(CONF_DEVICE_NAME) or "DTSU666"
        if self._reconfigure:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(),
                data_updates=data,
                title=name,
            )
        await self.async_set_unique_id(data.get(CONF_SERIAL_PORT) or data.get(CONF_HOST))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=name, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        return Dtsu666OptionsFlow()


class Dtsu666OptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        def _cur(key, default):
            return int(self.config_entry.options.get(key, self.config_entry.data.get(key, default)))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    **_timing_fields(
                        _cur(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                        _cur(CONF_STALENESS, DEFAULT_STALENESS),
                    ),
                }
            ),
        )
