// ============================================================================
// EV Thermal Management System - Model Root
// Master package: imports all view packages (each in its own file).
// View organization: Requirements / Architecture(BDD) / UseCases(UC) /
// Activities(ACT) / InternalBlocks(IBD) - separated into individual packages
// with distinct view types.
// ============================================================================
package EVThermalManagementSystem {
    private import ScalarValues::*;
    private import Base::*;

    // View packages (defined in 01..05 files)
    private import EVTM_Requirements::*;
    private import EVTM_Architecture::*;
    private import EVTM_UseCases::*;
    private import EVTM_Activities::*;
    private import EVTM_InternalBlocks::*;
}
