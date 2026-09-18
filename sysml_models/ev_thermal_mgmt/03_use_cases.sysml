// ============================================================================
// EV Thermal Management System - Use Case View (UC)
// View package: EVTM_UseCases  (stakeholder-use case associations)
// Actors are part def (SysML v2 rule); stakeholder-use case association is
// expressed with actor features inside use case def (OMG standard).
// ============================================================================
package EVTM_UseCases {
    private import ScalarValues::*;
    private import Base::*;
    private import EVTM_Architecture::*;

    // ---------- Actors (stakeholders / external systems) ----------
    part def Driver;
    part def Passenger;
    part def BatterySystem;
    part def PowertrainSystem;
    part def ServiceTechnician;
    part def Regulator;

    // Subject system
    part def EVThermalManagementSystem;

    // ---------- Use case definitions ----------
    use case def RegulateCabinTemperature {
        doc /* Driver or passenger adjusts cabin temperature to a comfort set point */
        subject system : EVThermalManagementSystem;
        actor driver : Driver;
        actor passenger : Passenger;
        include heatPump : HeatPumpHeating;
    }
    use case def CoolBattery {
        doc /* Battery cooling during fast charging or high discharge load */
        subject system : EVThermalManagementSystem;
        actor batterySystem : BatterySystem;
    }
    use case def HeatBatteryColdStart {
        doc /* Battery pre-heating at low ambient temperature before driving */
        subject system : EVThermalManagementSystem;
        actor batterySystem : BatterySystem;
        actor driver : Driver;
        include wasteHeat : WasteHeatRecovery;
    }
    use case def CoolPowertrain {
        doc /* Motor and inverter cooling under sustained load */
        subject system : EVThermalManagementSystem;
        actor powertrainSystem : PowertrainSystem;
    }
    use case def HeatPumpHeating {
        doc /* Cabin heating via heat pump to achieve high COP */
        subject system : EVThermalManagementSystem;
        actor driver : Driver;
        actor passenger : Passenger;
    }
    use case def WasteHeatRecovery {
        doc /* Recover powertrain waste heat to warm battery or cabin */
        subject system : EVThermalManagementSystem;
        actor powertrainSystem : PowertrainSystem;
    }
    use case def ThermalRunawayWarning {
        doc /* Detect thermal runaway precursors, warn driver and trigger protection */
        subject system : EVThermalManagementSystem;
        actor batterySystem : BatterySystem;
        actor driver : Driver;
    }
    use case def RemotePreconditioning {
        doc /* Pre-condition battery and cabin remotely before departure */
        subject system : EVThermalManagementSystem;
        actor driver : Driver;
        extend energyMode : EnergySavingMode;
    }
    use case def EnergySavingMode {
        doc /* Optimize thermal energy consumption in eco driving mode */
        subject system : EVThermalManagementSystem;
        actor driver : Driver;
    }
    use case def PeriodicServiceCheck {
        doc /* Service technician inspects refrigerant and coolant charge */
        subject system : EVThermalManagementSystem;
        actor technician : ServiceTechnician;
    }
}
