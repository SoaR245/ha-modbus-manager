"""Tests for cross-hub instantaneous power rates without Home Assistant installed."""

from __future__ import annotations

import ast
import asyncio
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "modbus_manager"


def _coordinator_class():
    path = COMPONENT / "combined_coordinator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    names = {
        "_extract_metric_record",
        "_convert_unit",
        "_operand_record",
        "_power_rate_battery_record",
        "_power_rate_metric",
    }
    original.bases = []
    original.body = [
        node for node in original.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {
        "Any": Any,
        "math": math,
        "er": SimpleNamespace(async_get=lambda hass: hass.registry),
        "SOURCE_ROLE_INVERTER": "inverter",
        "SOURCE_ROLE_IHM": "ihm",
    }
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), original],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)  # nosec B102
    return namespace["CombinedDeviceCoordinator"]


def _specs():
    path = COMPONENT / "combined_specs.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {
        "SOURCE_ROLE_INVERTER", "SOURCE_ROLE_IHM", "_CONSUMED_FORMULA",
        "_DAILY_CONSUMED_OPERANDS", "_TOTAL_CONSUMED_OPERANDS",
        "COMBINED_SENSOR_METRIC_SPECS",
    }
    assignments = []
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) else (
            node.target if isinstance(node, ast.AnnAssign) else None
        )
        if isinstance(target, ast.Name) and target.id in names:
            assignments.append(node)
    namespace = {"Any": Any}
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *assignments],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)  # nosec B102
    return namespace["COMBINED_SENSOR_METRIC_SPECS"]["inverter_ihm"]


def _register(name: str, value: float, unit: str = "W") -> dict[str, Any]:
    return {
        "processed_value": value,
        "register_config": {"unique_id": f"device_{name}", "unit_of_measurement": unit},
    }


class CombinedPowerRateTests(unittest.TestCase):
    def setUp(self):
        self.specs = _specs()
        self.coordinator = _coordinator_class()()
        self.source = SimpleNamespace(data={
            "devices": [{"type": "inverter", "prefix": "INV", "battery_config": "battery"}]
        })
        self.registry = SimpleNamespace(entities={})
        self.states = {}
        self.coordinator.hass = SimpleNamespace(
            registry=self.registry,
            states=SimpleNamespace(get=self.states.get),
            config_entries=SimpleNamespace(async_get_entry=lambda _: self.source),
        )
        self.coordinator.entry = SimpleNamespace(data={
            "source_entry_id_a": "inverter", "source_entry_id_b": "ihm"
        })
        self.coordinator._entry_id_for_role = lambda *_: "inverter"
        self.data = {
            "inverter": {
                "pv": _register("total_dc_power", 4000),
                "battery": _register("battery_power", 500),
            },
            "ihm": {
                "grid": _register("meter_active_power_raw", -1000),
                "load": _register("load_power", 5000),
            },
        }

    def rate(self, key: str):
        return self.coordinator._power_rate_metric(self.data, self.specs[key])

    def test_both_rates_use_ihm_grid_and_load(self):
        value, meta = self.rate("combined_self_consumption_rate")
        self.assertEqual(value, 87.5)
        self.assertEqual(meta["operands"]["grid"]["role"], "ihm")
        self.assertEqual(self.rate("combined_autarky_rate")[0], 70)

    def test_import_charge_clamping_and_zero_denominators(self):
        self.data["ihm"]["grid"] = _register("meter_active_power_raw", 1000)
        self.data["inverter"]["battery"] = _register("battery_power", -500)
        self.assertEqual(self.rate("combined_self_consumption_rate")[0], 100)
        self.assertEqual(self.rate("combined_autarky_rate")[0], 80)
        self.data["ihm"]["grid"] = _register("meter_active_power_raw", -6000)
        self.assertEqual(self.rate("combined_autarky_rate")[0], 0)
        self.data["inverter"]["pv"] = _register("total_dc_power", 0)
        self.data["ihm"]["load"] = _register("load_power", 0)
        self.assertEqual(self.rate("combined_self_consumption_rate")[0], 0)
        self.assertEqual(self.rate("combined_autarky_rate")[0], 100)

    def test_missing_and_invalid_inputs_do_not_become_zero(self):
        del self.data["ihm"]["grid"]
        self.assertEqual(self.rate("combined_self_consumption_rate"), (None, None))
        self.data["ihm"]["grid"] = _register("meter_active_power_raw", math.nan)
        self.assertEqual(self.rate("combined_autarky_rate"), (None, None))
        self.data["ihm"]["grid"] = _register("meter_active_power_raw", 0)
        del self.data["inverter"]["battery"]
        self.assertEqual(self.rate("combined_self_consumption_rate"), (None, None))
        self.data["inverter"]["battery"] = _register("battery_power", 100, "")
        self.assertEqual(self.rate("combined_self_consumption_rate"), (None, None))

    def test_explicit_no_battery_and_battery_device(self):
        del self.data["inverter"]["battery"]
        self.source.data["devices"][0]["battery_config"] = "none"
        self.assertEqual(self.rate("combined_self_consumption_rate")[0], 75)
        self.source.data["devices"].append({"type": "battery"})
        self.assertEqual(self.rate("combined_self_consumption_rate"), (None, None))

    def test_legacy_calculated_battery_and_unit_conversion(self):
        del self.data["inverter"]["battery"]
        self.data["inverter"]["pv"] = _register("total_dc_power", 4, "kW")
        self.registry.entities["sensor.inv_battery"] = SimpleNamespace(
            config_entry_id="inverter", domain="sensor",
            unique_id="INV_battery_power", entity_id="sensor.inv_battery",
        )
        self.states["sensor.inv_battery"] = SimpleNamespace(
            state="0.5", attributes={"unit_of_measurement": "kW"}
        )
        value, meta = self.rate("combined_self_consumption_rate")
        self.assertEqual(value, 87.5)
        self.assertEqual(meta["operands"]["battery"]["unit"], "W")
        self.assertEqual(meta["operands"]["battery"]["value"], 500)
        self.states["sensor.inv_battery"].state = "unavailable"
        self.assertEqual(self.rate("combined_self_consumption_rate"), (None, None))

    def test_combined_setup_creates_both_rate_sensors(self):
        path = COMPONENT / "sensor.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        setup = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
        )
        namespace = {
            "DOMAIN": "modbus_manager",
            "CONF_ENTRY_TYPE": "entry_type",
            "ENTRY_TYPE_COMBINED_DEVICE": "combined_device",
            "COMBINED_SENSOR_METRIC_SPECS": {"inverter_ihm": self.specs},
            "combination_type_for_entry": lambda *_: "inverter_ihm",
            "CombinedPairTypeSensor": lambda *_: None,
            "CombinedSumSensor": lambda _c, _e, key, _name, unit: (key, unit),
        }
        module = ast.Module(
            body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), setup],
            type_ignores=[],
        )
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)  # nosec B102
        entry = SimpleNamespace(entry_id="combined", data={"entry_type": "combined_device"})
        hass = SimpleNamespace(data={"modbus_manager": {"combined": {"coordinator": object()}}})
        added = []
        asyncio.run(namespace["async_setup_entry"](hass, entry, added.extend))
        self.assertIn(("combined_self_consumption_rate", "%"), added)
        self.assertIn(("combined_autarky_rate", "%"), added)


if __name__ == "__main__":
    unittest.main()
