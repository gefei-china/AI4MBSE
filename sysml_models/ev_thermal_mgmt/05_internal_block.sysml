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
        connect coolantIn to pump.inlet;
        connect pump.outlet to chiller.coolantIn;
        connect chiller.coolantOut to valve.commonIn;
        connect valve.branchAOut to coldPlate.coolantIn;
        connect valve.branchBOut to heater.coolantIn;
        connect coldPlate.coolantOut to coolantOut;
        connect heater.coolantOut to coolantOut;
        connect refrigerantIn to chiller.refrigerantIn;
        connect chiller.refrigerantOut to refrigerantOut;
        connect sensor.signal to controlBus;
    }
}
