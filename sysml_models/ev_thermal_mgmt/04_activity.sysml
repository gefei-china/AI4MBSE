// ============================================================================
// EV Thermal Management System - Activity View (ACT)
// View package: EVTM_Activities  (battery thermal management control flow)
// Swimlanes are expressed with perform action inside part usages (each part
// is a lane: Sensor / Controller / Actuator / Protection / Battery).
// Control flow: guarded succession; Data flow: flow A.out to B.in.
// ============================================================================
package EVTM_Activities {
    private import ScalarValues::*;
    private import Base::*;

    // ---------- Swimlane performer types ----------
    part def TempSensor;
    part def ThermalManagementController;
    part def ThermalActuator;
    part def ProtectionSystem;
    part def BatterySystem;

    // ---------- Action definitions (parameters only) ----------
    action def SenseBatteryTemp {
        out temperature : Real;
    }
    action def EvaluateTempBand {
        in temperature : Real;
        out isOverLimit : Boolean;
        out isUnderLimit : Boolean;
        out deviation : Real;
    }
    action def EngageCooling {
        in deviation : Real;
    }
    action def EngageHeating {
        in deviation : Real;
    }
    action def MaintainPassive {
    }
    action def AdjustFlowRate {
        in deviation : Real;
        out coolingRate : Real;
    }
    action def RunProtectionCheck {
        out riskLevel : Integer;
    }
    action def TriggerProtection {
        in riskLevel : Integer;
    }
    action def UpdateControlState {
        in state : String;
    }

    // ---------- Battery thermal management control activity ----------
    action def BatteryThermalControl {
        doc /* Battery thermal management control loop. Swimlanes: Sensor / Controller / Actuator / Protection / Battery */
        // Lane 1: Sensor
        part sensor : TempSensor {
            perform action senseTemp : SenseBatteryTemp;
        }
        // Lane 2: Controller
        part controller : ThermalManagementController {
            perform action evaluate : EvaluateTempBand;
            perform action adjustFlow : AdjustFlowRate;
            perform action protectionCheck : RunProtectionCheck;
            perform action updateState : UpdateControlState;
        }
        // Lane 3: Actuator
        part actuator : ThermalActuator {
            perform action cool : EngageCooling;
            perform action heat : EngageHeating;
            perform action passive : MaintainPassive;
        }
        // Lane 4: Protection
        part protection : ProtectionSystem {
            perform action trigger : TriggerProtection;
        }
        // Lane 5: Battery (managed context)
        part battery : BatterySystem {
            attribute temperature : Real;
        }

        // Merge node for mode selection
        merge modeSelected;

        // Control flow (guarded succession)
        first senseTemp then evaluate;
        first evaluate if evaluate.isOverLimit then cool;
        first evaluate if evaluate.isUnderLimit then heat;
        first evaluate else passive;
        first cool then modeSelected;
        first heat then modeSelected;
        first passive then modeSelected;
        first modeSelected then adjustFlow;
        first adjustFlow then protectionCheck;
        first protectionCheck if protectionCheck.riskLevel >= 2 then trigger;
        else updateState;
        first trigger then updateState;

        // Data flow (object flow)
        flow senseTemp.temperature to evaluate.temperature;
        flow senseTemp.temperature to battery.temperature;
        flow evaluate.deviation to cool.deviation;
        flow evaluate.deviation to heat.deviation;
    }
}
