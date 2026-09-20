// ============================================================================
// EV Thermal Management System - Architecture View (BDD)
// View package: EVTM_Architecture  (block definition diagram: composition)
// Block definition with combination features (parts) + satisfaction trace.
// ============================================================================
package EVTM_Architecture {
    private import ScalarValues::*;
    private import Base::*;
    private import EVTM_Requirements::*;

    // ---------- System context ----------
    part def Vehicle {
        part thermalManagement : EVThermalManagementSystem;
        part powertrain : PowertrainSystem;
        part chassis : ChassisSystem;
    }

    // ---------- Top system block (combination features) ----------
    part def EVThermalManagementSystem {
        part batteryThermal : BatteryThermalManagement;
        part cabinThermal : CabinThermalManagement;
        part powertrainThermal : PowertrainThermalManagement;
        part refrigerantLoop : RefrigerantLoop;
        part coolantLoop : CoolantLoop;
        part controller : ThermalManagementController;
        part energyStorage : HighVoltageBattery;
    }

    // ---------- Subsystem blocks ----------
    part def BatteryThermalManagement {
        part coolantPump : CoolantPump;
        part chiller : RefrigerantChiller;
        part heater : ElectricHeater;
        part valve : ThreeWayValve;
        part coldPlate : ColdPlate;
        part tempSensor : TempSensor;
        attribute targetTemp : Real = 25.0;
        attribute maxDeltaTemp : Real = 5.0;
    }
    part def CabinThermalManagement {
        part hvac : HVACUnit;
        part condenser : Condenser;
        part evaporator : Evaporator;
        part heaterCore : HeaterCore;
        part blower : Blower;
        attribute setPoint : Real = 24.0;
    }
    part def PowertrainThermalManagement {
        part motorCooling : CoolantLoop;
        part oilCooler : OilCooler;
        part radiator : Radiator;
    }
    part def RefrigerantLoop {
        part compressor : ElectricCompressor;
        part condenser : Condenser;
        part evaporator : Evaporator;
        part expansionValve : ExpansionValve;
        part fourWayValve : FourWayValve;
        part chiller : RefrigerantChiller;
        part accumulator : Accumulator;
    }
    part def CoolantLoop {
        part pump : CoolantPump;
        part reservoir : CoolantReservoir;
        part heater : ElectricHeater;
        part radiator : Radiator;
        part threeWayValve : ThreeWayValve;
    }
    part def ThermalManagementController {
        part modeSelector : ModeSelector;
        part tempMonitor : TempMonitor;
        part pumpDriver : PumpDriver;
        part compressorDriver : CompressorDriver;
        part valveDriver : ValveDriver;
    }
    part def HighVoltageBattery {
        part cells : BatteryCell;
        part bms : BatteryManagementSystem;
        part coolingPlate : ColdPlate;
    }

    // ---------- Leaf component blocks ----------
    part def CoolantPump;
    part def RefrigerantChiller;
    part def ElectricHeater;
    part def ThreeWayValve;
    part def ColdPlate;
    part def TempSensor;
    part def HVACUnit;
    part def Condenser;
    part def Evaporator;
    part def HeaterCore;
    part def Blower;
    part def OilCooler;
    part def Radiator;
    part def ElectricCompressor;
    part def ExpansionValve;
    part def FourWayValve;
    part def Accumulator;
    part def CoolantReservoir;
    part def ModeSelector;
    part def TempMonitor;
    part def PumpDriver;
    part def CompressorDriver;
    part def ValveDriver;
    part def BatteryCell;
    part def BatteryManagementSystem;
    part def PowertrainSystem;
    part def ChassisSystem;

    // ---------- Satisfaction trace (requirements <- design blocks) ----------
    satisfaction satisfy ReqBatteryTempRange by BatteryThermalManagement;
    satisfaction satisfy ReqBatteryTempDelta by BatteryThermalManagement;
    satisfaction satisfy ReqCabinComfort by CabinThermalManagement;
    satisfaction satisfy ReqColdStart by BatteryThermalManagement;
    satisfaction satisfy ReqHeatPumpCOP by CabinThermalManagement;
    satisfaction satisfy ReqEnergyBudget by EVThermalManagementSystem;
    satisfaction satisfy ReqThermalRunawayProtection by BatteryThermalManagement;
    satisfaction satisfy ReqIntegrationWeight by EVThermalManagementSystem;
    satisfaction satisfy ReqReliability by CoolantLoop;
}
