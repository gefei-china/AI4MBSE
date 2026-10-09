// ============================================================================
// EV Thermal Management System - Requirements View (REQ)
// View package: EVTM_Requirements  (requirement statements + stakeholder needs)
// SysML v2 (OMG) textual notation. English/ASCII only (Level 1 conservative).
// ============================================================================
package EVTM_Requirements {
    private import ScalarValues::*;
    private import Base::*;

    // ---------- Stakeholders (owners of needs) ----------
    part def Driver;
    part def Passenger;
    part def BatterySystem;
    part def EVOperator;
    part def Regulator;
    part def ServiceTechnician;

    // ---------- Requirement statements ----------
    requirement ReqBatteryTempRange {
        doc /* Battery cell operating temperature range under discharge and charge */
        attribute reqId = "REQ-1";
        attribute text = "Battery cell operating temperature shall be maintained within 15 C to 40 C during discharge, and within 0 C to 45 C during charge.";
    }
    requirement ReqBatteryTempDelta {
        doc /* Maximum temperature difference between any two cells */
        attribute reqId = "REQ-2";
        attribute text = "The temperature difference between any two battery cells shall not exceed 5 C.";
    }
    requirement ReqCabinComfort {
        doc /* Cabin thermal comfort */
        attribute reqId = "REQ-3";
        attribute text = "Cabin temperature shall be adjustable within 22 C to 26 C and shall reach the target within 15 minutes.";
    }
    requirement ReqColdStart {
        doc /* Low temperature cold start capability */
        attribute reqId = "REQ-4";
        attribute text = "At -20 C ambient temperature, the battery shall support charge and discharge within 30 minutes after cold start heating.";
    }
    requirement ReqHeatPumpCOP {
        doc /* Heat pump heating efficiency */
        attribute reqId = "REQ-5";
        attribute text = "The heat pump heating coefficient of performance shall be no less than 2.0 at -10 C ambient temperature.";
    }
    requirement ReqEnergyBudget {
        doc /* Energy consumption budget of the thermal management system */
        attribute reqId = "REQ-6";
        attribute text = "Total thermal management energy consumption shall not exceed 15 percent of vehicle usable energy under the CLTC cycle.";
    }
    requirement ReqThermalRunawayProtection {
        doc /* Thermal runaway warning and protection */
        attribute reqId = "REQ-7";
        attribute text = "Thermal runaway warning shall be issued at least 5 minutes before hazard, and protection actions shall be triggered automatically.";
    }
    requirement ReqIntegrationWeight {
        doc /* Weight budget of the thermal management system */
        attribute reqId = "REQ-8";
        attribute text = "The total mass of the thermal management system shall not exceed 80 kg.";
    }
    requirement ReqReliability {
        doc /* Leak-free reliability over vehicle lifetime */
        attribute reqId = "REQ-9";
        attribute text = "The thermal management system shall operate without coolant leakage for 15 years or 300,000 km.";
    }

    // ---------- Requirement relationships (refines / traces) ----------
    requirement ReqBatteryTempDelta :> ReqBatteryTempRange;
    requirement ReqColdStart :> ReqBatteryTempRange;
    requirement ReqThermalRunawayProtection :> ReqBatteryTempRange;
    requirement ReqHeatPumpCOP :> ReqCabinComfort;
    dependency from ReqReliability to ReqBatteryTempRange;
    dependency from ReqEnergyBudget to ReqCabinComfort;
}
