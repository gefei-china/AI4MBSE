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
evaluate;
if evaluate.isOverLimit {
        action cool;
      }
if evaluate.isUnderLimit {
        action heat;
      }
// ⚠️ 语义缺口（2026-10-08 标注）：以下 evaluate; / modeSelected; 序列原写作
//    `first evaluate else passive; first passive then modeSelected;`
//    但 SysML v2 无 first 关键字，且原文**未给出判定条件**（只有分支名，没有 if 条件）。
//    正确写法需补条件，例：if evaluate.result == Mode::active { action evaluate; } else { action passive; }
//    当前只做「去 first 关键字」的最小修正；passive 分支的判定条件**需建模方补充**。
//    这是已知建模缺口（不是语法错误），故本文件仍有语义类诊断。
evaluate;
modeSelected;
modeSelected;
modeSelected;
adjustFlow;
protectionCheck;
if protectionCheck.riskLevel >= 2 {
        action trigger;
      }
} else {
        action updateState;
      }
updateState;

        // Data flow (object flow)
        flow senseTemp.temperature to evaluate.temperature;
        flow senseTemp.temperature to battery.temperature;
        flow evaluate.deviation to cool.deviation;
        flow evaluate.deviation to heat.deviation;
    }
}
