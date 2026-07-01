"""Config flow for SmartIR — 6-step UI wizard with device list selection."""
from __future__ import annotations

import json
import logging
import os

import aiofiles
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import COMPONENT_ABS_DIR, Helper
from .const import (
    ALL_CONTROLLERS_SENTINEL,
    CONF_CONTROLLER_DATA,
    CONF_DELAY,
    CONF_DEVICE_CLASS,
    CONF_DEVICE_CODE,
    CONF_DEVICE_NAME,
    CONF_HUMIDITY_SENSOR,
    CONF_PLATFORM,
    CONF_POWER_SENSOR,
    CONF_POWER_SENSOR_RESTORE_STATE,
    CONF_SOURCE_NAMES,
    CONF_TEMPERATURE_SENSOR,
    CONTROLLER_HINTS,
    DEFAULT_DELAY,
    DEFAULT_DEVICE_NAMES,
    ENTITY_BASED_CONTROLLERS,
    INDEX_FILENAME,
    MANUAL_ENTRY_SENTINEL,
    PLATFORM_SUBDIR,
    PLATFORMS,
)

DOMAIN = "smartir"
_LOGGER = logging.getLogger(__name__)


# ── Module-level helpers ──────────────────────────────────────────────────────

async def _load_device_json(platform: str, device_code: int) -> dict | None:
    """Return device JSON dict, downloading from GitHub if not cached locally."""
    subdir = PLATFORM_SUBDIR[platform]
    codes_dir = os.path.join(COMPONENT_ABS_DIR, "codes", subdir)
    os.makedirs(codes_dir, exist_ok=True)
    path = os.path.join(codes_dir, f"{device_code}.json")

    if not os.path.exists(path):
        source = (
            "https://raw.githubusercontent.com/"
            f"smartHomeHub/SmartIR/master/codes/{subdir}/{device_code}.json"
        )
        try:
            await Helper.downloader(source, path)
        except Exception:
            return None

    try:
        async with aiofiles.open(path, mode="r") as f:
            return json.loads(await f.read())
    except Exception:
        return None


def _controller_schema(
    controller_type: str,
    current_data: str = "",
    current_delay: str = DEFAULT_DELAY,
) -> vol.Schema:
    """Build controller step schema; entity picker for Broadlink/Xiaomi, text for rest."""
    if controller_type in ENTITY_BASED_CONTROLLERS:
        data_field: object = EntitySelector(EntitySelectorConfig(domain="remote"))
    else:
        data_field = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))

    fields: dict = {}
    if current_data:
        fields[
            vol.Required(
                CONF_CONTROLLER_DATA,
                description={"suggested_value": current_data},
            )
        ] = data_field
    else:
        fields[vol.Required(CONF_CONTROLLER_DATA)] = data_field

    fields[
        vol.Optional(
            CONF_DELAY,
            description={"suggested_value": current_delay},
        )
    ] = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))

    return vol.Schema(fields)


def _options_schema(platform: str, defaults: dict | None = None) -> vol.Schema:
    """Build optional-sensors schema for the given platform."""
    d = defaults or {}

    def _opt(key: str) -> vol.Optional:
        val = d.get(key)
        if val is not None and val != "":
            return vol.Optional(key, description={"suggested_value": val})
        return vol.Optional(key)

    if platform == "climate":
        return vol.Schema(
            {
                _opt(CONF_TEMPERATURE_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                _opt(CONF_HUMIDITY_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                _opt(CONF_POWER_SENSOR): EntitySelector(EntitySelectorConfig()),
                vol.Optional(
                    CONF_POWER_SENSOR_RESTORE_STATE,
                    default=bool(d.get(CONF_POWER_SENSOR_RESTORE_STATE, False)),
                ): BooleanSelector(),
            }
        )

    if platform == "media_player":
        src_existing = d.get(CONF_SOURCE_NAMES, {})
        src_str = (
            json.dumps(src_existing)
            if isinstance(src_existing, dict) and src_existing
            else ""
        )
        fields: dict = {
            _opt(CONF_POWER_SENSOR): EntitySelector(EntitySelectorConfig()),
            vol.Optional(
                CONF_DEVICE_CLASS,
                description={"suggested_value": d.get(CONF_DEVICE_CLASS, "tv")},
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
        }
        src_key = (
            vol.Optional(
                CONF_SOURCE_NAMES,
                description={"suggested_value": src_str},
            )
            if src_str
            else vol.Optional(CONF_SOURCE_NAMES)
        )
        fields[src_key] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
        )
        return vol.Schema(fields)

    # fan and light
    return vol.Schema(
        {_opt(CONF_POWER_SENSOR): EntitySelector(EntitySelectorConfig())}
    )


def _parse_options(platform: str, user_input: dict) -> dict:
    """Clean and coerce options form input before storing."""
    out: dict = {}

    def _maybe(key: str) -> None:
        val = user_input.get(key, "")
        if isinstance(val, str):
            val = val.strip()
        if val:
            out[key] = val

    if platform == "climate":
        _maybe(CONF_TEMPERATURE_SENSOR)
        _maybe(CONF_HUMIDITY_SENSOR)
        _maybe(CONF_POWER_SENSOR)
        out[CONF_POWER_SENSOR_RESTORE_STATE] = bool(
            user_input.get(CONF_POWER_SENSOR_RESTORE_STATE, False)
        )
    elif platform == "media_player":
        _maybe(CONF_POWER_SENSOR)
        out[CONF_DEVICE_CLASS] = (
            (user_input.get(CONF_DEVICE_CLASS) or "tv").strip() or "tv"
        )
        src_raw = user_input.get(CONF_SOURCE_NAMES, "")
        if isinstance(src_raw, str) and src_raw.strip():
            try:
                parsed = json.loads(src_raw.strip())
                if isinstance(parsed, dict):
                    out[CONF_SOURCE_NAMES] = parsed
            except json.JSONDecodeError:
                pass
        elif isinstance(src_raw, dict) and src_raw:
            out[CONF_SOURCE_NAMES] = src_raw
    else:  # fan, light
        _maybe(CONF_POWER_SENSOR)

    return out


def _device_placeholders(device_data: dict, platform: str) -> dict:
    """Build description_placeholders dict for the options step."""
    p: dict = {
        "manufacturer": device_data.get("manufacturer", "—"),
        "models": ", ".join(device_data.get("supportedModels", [])) or "—",
        "controller_type": device_data.get("supportedController", "—"),
        "device_code": str(device_data.get("device_code", "—")),
    }
    if platform == "climate":
        p["temp_range"] = (
            f"{device_data.get('minTemperature', '—')} – "
            f"{device_data.get('maxTemperature', '—')} °C"
        )
        p["operation_modes"] = ", ".join(device_data.get("operationModes", []))
        p["fan_modes"] = ", ".join(device_data.get("fanModes", []))
        swing = device_data.get("swingModes", [])
        p["swing_modes"] = ", ".join(swing) if swing else "none"
    return p


# ── Config Flow ───────────────────────────────────────────────────────────────


class SmartIRConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle multi-step UI config flow for SmartIR."""

    VERSION = 1

    def __init__(self) -> None:
        self._platform: str = ""
        self._controller_filter: str = ALL_CONTROLLERS_SENTINEL
        self._manufacturer: str = ""
        self._device_code: int = 0
        self._device_data: dict = {}
        self._device_name: str = ""
        self._controller_data: str = ""
        self._delay: str = DEFAULT_DELAY
        self._index_cache: list[dict] | None = None

    # ── Index helpers ─────────────────────────────────────────────────────────

    async def _get_index(self) -> list[dict]:
        """Load and cache index entries for the current platform."""
        if self._index_cache is not None:
            return self._index_cache
        path = os.path.join(COMPONENT_ABS_DIR, INDEX_FILENAME)
        try:
            async with aiofiles.open(path, mode="r") as f:
                data = json.loads(await f.read())
            self._index_cache = data.get(self._platform, [])
        except Exception:
            _LOGGER.warning("Could not load %s", INDEX_FILENAME)
            self._index_cache = []
        return self._index_cache

    def _filtered(self, entries: list[dict]) -> list[dict]:
        if self._controller_filter == ALL_CONTROLLERS_SENTINEL:
            return entries
        return [e for e in entries if e.get("controller") == self._controller_filter]

    # ── Step 1: platform ──────────────────────────────────────────────────────

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            self._platform = user_input["platform"]
            return await self.async_step_controller_filter()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("platform"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"label": v, "value": k}
                                for k, v in PLATFORMS.items()
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # ── Step 2: controller filter ─────────────────────────────────────────────

    async def async_step_controller_filter(
        self, user_input: dict | None = None
    ) -> FlowResult:
        entries = await self._get_index()

        if user_input is not None:
            self._controller_filter = user_input.get(
                "controller_type", ALL_CONTROLLERS_SENTINEL
            )
            return await self.async_step_manufacturer()

        available = sorted({e["controller"] for e in entries})
        options = [{"label": "Any (show all)", "value": ALL_CONTROLLERS_SENTINEL}]
        options += [{"label": c, "value": c} for c in available]

        return self.async_show_form(
            step_id="controller_filter",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "controller_type", default=ALL_CONTROLLERS_SENTINEL
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )

    # ── Step 3: manufacturer ──────────────────────────────────────────────────

    async def async_step_manufacturer(
        self, user_input: dict | None = None
    ) -> FlowResult:
        entries = self._filtered(await self._get_index())
        manufacturers = sorted({e["manufacturer"] for e in entries})

        if user_input is not None:
            sel = user_input.get("manufacturer", "")
            if sel == MANUAL_ENTRY_SENTINEL:
                return await self.async_step_device_manual()
            self._manufacturer = sel
            return await self.async_step_device_select()

        options = [
            {"label": "— Enter code manually —", "value": MANUAL_ENTRY_SENTINEL}
        ]
        options += [{"label": m, "value": m} for m in manufacturers]

        return self.async_show_form(
            step_id="manufacturer",
            data_schema=vol.Schema(
                {
                    vol.Required("manufacturer"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
        )

    # ── Step 4a: device from index list ───────────────────────────────────────

    async def async_step_device_select(
        self, user_input: dict | None = None
    ) -> FlowResult:
        entries = [
            e
            for e in self._filtered(await self._get_index())
            if e.get("manufacturer") == self._manufacturer
        ]
        errors: dict[str, str] = {}

        if user_input is not None:
            sel = user_input.get("device_code", "")
            if sel == MANUAL_ENTRY_SENTINEL:
                return await self.async_step_device_manual()
            try:
                device_code = int(sel)
            except (ValueError, TypeError):
                errors["device_code"] = "invalid_device_code"
            else:
                device_data = await _load_device_json(self._platform, device_code)
                if device_data is None:
                    errors["device_code"] = "invalid_device_code"
                else:
                    self._device_code = device_code
                    self._device_data = device_data
                    models = device_data.get("supportedModels", [])
                    self._device_name = (
                        f"{self._manufacturer} {models[0]}"
                        if models
                        else DEFAULT_DEVICE_NAMES[self._platform]
                    )
                    return await self.async_step_device_name()

        options = [
            {"label": "— Enter code manually —", "value": MANUAL_ENTRY_SENTINEL}
        ]
        for e in sorted(entries, key=lambda x: x["code"]):
            models_str = ", ".join(e.get("models", []))
            options.append(
                {"label": f"{e['code']} — {models_str}", "value": str(e["code"])}
            )

        return self.async_show_form(
            step_id="device_select",
            data_schema=vol.Schema(
                {
                    vol.Required("device_code"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
            description_placeholders={"manufacturer": self._manufacturer},
            errors=errors,
        )

    # ── Step 4b: manual code entry ────────────────────────────────────────────

    async def async_step_device_manual(
        self, user_input: dict | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                device_code = int(user_input["device_code"])
                if device_code <= 0:
                    raise ValueError("non-positive")
            except (ValueError, KeyError, TypeError):
                errors["device_code"] = "invalid_device_code"
            else:
                device_data = await _load_device_json(self._platform, device_code)
                if device_data is None:
                    errors["device_code"] = "invalid_device_code"
                else:
                    self._device_code = device_code
                    self._device_data = device_data
                    manufacturer = device_data.get("manufacturer", "")
                    models = device_data.get("supportedModels", [])
                    self._manufacturer = manufacturer
                    self._device_name = (
                        f"{manufacturer} {models[0]}"
                        if models
                        else DEFAULT_DEVICE_NAMES[self._platform]
                    )
                    return await self.async_step_device_name()

        return self.async_show_form(
            step_id="device_manual",
            data_schema=vol.Schema(
                {
                    vol.Required("device_code"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.NUMBER)
                    )
                }
            ),
            errors=errors,
        )

    # ── Step 4c: confirm / edit name ──────────────────────────────────────────

    async def async_step_device_name(
        self, user_input: dict | None = None
    ) -> FlowResult:
        if user_input is not None:
            name = (user_input.get("name") or "").strip() or self._device_name
            self._device_name = name
            return await self.async_step_controller()

        return self.async_show_form(
            step_id="device_name",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "name",
                        description={"suggested_value": self._device_name},
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
                }
            ),
            description_placeholders={
                "manufacturer": self._device_data.get("manufacturer", ""),
                "models": ", ".join(self._device_data.get("supportedModels", [])),
                "controller_type": self._device_data.get("supportedController", ""),
                "device_code": str(self._device_code),
            },
        )

    # ── Step 5: controller data ───────────────────────────────────────────────

    async def async_step_controller(
        self, user_input: dict | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        controller_type = self._device_data.get("supportedController", "")
        hint = CONTROLLER_HINTS.get(controller_type, "Enter the controller connection string.")

        if user_input is not None:
            controller_data = user_input.get(CONF_CONTROLLER_DATA, "")
            if isinstance(controller_data, str):
                controller_data = controller_data.strip()
            if not controller_data:
                errors[CONF_CONTROLLER_DATA] = "controller_data_required"
            else:
                self._controller_data = controller_data
                delay_raw = user_input.get(CONF_DELAY, DEFAULT_DELAY)
                try:
                    float(delay_raw)
                    self._delay = str(delay_raw)
                except (ValueError, TypeError):
                    self._delay = DEFAULT_DELAY

                uid = (
                    f"smartir_{self._platform}_{self._device_code}"
                    f"_{self._controller_data}"
                )
                await self.async_set_unique_id(uid)
                self._abort_if_unique_id_configured()

                return await self.async_step_options_platform()

        return self.async_show_form(
            step_id="controller",
            data_schema=_controller_schema(
                controller_type, self._controller_data, self._delay
            ),
            description_placeholders={
                "controller_type": controller_type,
                "controller_hint": hint,
            },
            errors=errors,
        )

    # ── Step 6: optional sensors / platform settings ──────────────────────────

    async def async_step_options_platform(
        self, user_input: dict | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self._create_entry(user_input)

        return self.async_show_form(
            step_id="options_platform",
            data_schema=_options_schema(self._platform),
            description_placeholders=_device_placeholders(
                self._device_data, self._platform
            ),
        )

    # ── Entry creation ────────────────────────────────────────────────────────

    def _create_entry(self, options_input: dict) -> FlowResult:
        try:
            delay_float = float(self._delay)
        except (ValueError, TypeError):
            delay_float = 0.5

        # Immutable identification stored in data
        data: dict = {
            CONF_PLATFORM:    self._platform,
            CONF_DEVICE_CODE: self._device_code,
            CONF_DEVICE_NAME: self._device_name,
        }

        # Mutable settings stored in options (editable via options flow)
        options: dict = {
            CONF_CONTROLLER_DATA: self._controller_data,
            CONF_DELAY:           delay_float,
        }
        options.update(_parse_options(self._platform, options_input))

        mfr = self._device_data.get("manufacturer", "SmartIR")
        title = f"{mfr} — {self._device_name}"
        return self.async_create_entry(title=title, data=data, options=options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SmartIROptionsFlow:
        return SmartIROptionsFlow(config_entry)


# ── Options Flow ──────────────────────────────────────────────────────────────


class SmartIROptionsFlow(OptionsFlow):
    """Edit controller and sensor settings for an existing SmartIR entry."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._platform: str = config_entry.data.get(CONF_PLATFORM, "")
        self._device_code: int = config_entry.data.get(CONF_DEVICE_CODE, 0)
        self._device_data: dict = {}
        # Pre-fill from current options
        self._controller_data: str = config_entry.options.get(CONF_CONTROLLER_DATA, "")
        self._delay: str = str(config_entry.options.get(CONF_DELAY, DEFAULT_DELAY))

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if not self._device_data:
            self._device_data = (
                await _load_device_json(self._platform, self._device_code) or {}
            )
        return await self.async_step_controller()

    async def async_step_controller(
        self, user_input: dict | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        controller_type = self._device_data.get("supportedController", "")
        hint = CONTROLLER_HINTS.get(controller_type, "Enter the controller connection string.")

        if user_input is not None:
            controller_data = user_input.get(CONF_CONTROLLER_DATA, "")
            if isinstance(controller_data, str):
                controller_data = controller_data.strip()
            if not controller_data:
                errors[CONF_CONTROLLER_DATA] = "controller_data_required"
            else:
                self._controller_data = controller_data
                delay_raw = user_input.get(CONF_DELAY, DEFAULT_DELAY)
                try:
                    float(delay_raw)
                    self._delay = str(delay_raw)
                except (ValueError, TypeError):
                    self._delay = DEFAULT_DELAY
                return await self.async_step_options_platform()

        return self.async_show_form(
            step_id="controller",
            data_schema=_controller_schema(
                controller_type, self._controller_data, self._delay
            ),
            description_placeholders={
                "controller_type": controller_type,
                "controller_hint": hint,
            },
            errors=errors,
        )

    async def async_step_options_platform(
        self, user_input: dict | None = None
    ) -> FlowResult:
        if user_input is not None:
            try:
                delay_float = float(self._delay)
            except (ValueError, TypeError):
                delay_float = 0.5

            updated: dict = {
                CONF_CONTROLLER_DATA: self._controller_data,
                CONF_DELAY:           delay_float,
            }
            updated.update(_parse_options(self._platform, user_input))
            return self.async_create_entry(title="", data=updated)

        return self.async_show_form(
            step_id="options_platform",
            data_schema=_options_schema(self._platform, defaults=self._entry.options),
            description_placeholders=_device_placeholders(
                self._device_data, self._platform
            ),
        )
