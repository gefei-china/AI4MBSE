// ============================================================================
// EV Thermal Management System - Internal Block Definition View (IBD)
// View package: EVTM_InternalBlocks  (BatteryThermalManagement internals)
// Ports, part properties and connectors of the battery thermal subsystem.
// ============================================================================
package EVTM_InternalBlocks {
    private import ScalarValues::*;
    private import Base::*;

    // ---------- Port definitions ----------
    port def CoolantPort {
        attribute flowRate : Real;
        attribute temperature : Real;
    }
    port def RefrigerantPort {
        attribute pressure : Real;
        attribute enthalpy : Real;
    }
    port def SignalPort {
        attribute value : Real;
    }

    // ---------- Component blocks ----------
    part def CoolantPump {
        port inlet : CoolantPort;
        port outlet : CoolantPort;
        port control : SignalPort;
        attribute ratedFlow : Real;
    }
    part def RefrigerantChiller {
        port coolantIn : CoolantPort;
        port coolantOut : CoolantPort;
        port refrigerantIn : RefrigerantPort;
        port refrigerantOut : RefrigerantPort;
        attribute heatTransferCapacity : Real;
    }
    part def ElectricHeater {
        port coolantIn : CoolantPort;
        port coolantOut : CoolantPort;
        port control : SignalPort;
        attribute maxPower : Real;
    }
    part def ThreeWayValve {
        port commonIn : CoolantPort;
        port branchAOut : CoolantPort;
        port branchBOut : CoolantPort;
        port control : SignalPort;
    }
    part def ColdPlate {
        port coolantIn : CoolantPort;
        port coolantOut : CoolantPort;
        attribute thermalResistance : Real;
    }
    part def TempSensor {
        port signal : SignalPort;
        attribute measuredTemp : Real;
    }

    // ---------- Internal block definition (IBD) ----------
    part def BatteryThermalManagement {
        // Internal parts
        part pump : CoolantPump;
        part chiller : RefrigerantChiller;
        part heater : ElectricHeater;
        part valve : ThreeWayValve;
        part coldPlate : ColdPlate;
        part sensor : TempSensor;

        // Boundary ports
        port coolantIn : CoolantPort;
        port coolantOut : CoolantPort;
        port refrigerantIn : RefrigerantPort;
        port refrigerantOut : RefrigerantPort;
        port controlBus : SignalPort;

        // Connectors (internal structure)
        connector c_coolant_in from coolantIn to pump.inlet;
        connector c_pump_chiller from pump.outlet to chiller.coolantIn;
        connector c_chiller_valve from chiller.coolantOut to valve.commonIn;
        connector c_valve_plate from valve.branchAOut to coldPlate.coolantIn;
        connector c_valve_heater from valve.branchBOut to heater.coolantIn;
        connector c_plate_out from coldPlate.coolantOut to coolantOut;
        connector c_heater_out from heater.coolantOut to coolantOut;
        connector c_refrigerant_in from refrigerantIn to chiller.refrigerantIn;
        connector c_refrigerant_out from chiller.refrigerantOut to refrigerantOut;
        connector c_sensor_control from sensor.signal to controlBus;
    }
}
