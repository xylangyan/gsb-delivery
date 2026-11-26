require_relative "handover_audit/case"
require_relative "handover_audit/provenance"
require_relative "handover_audit/engine"
require_relative "handover_audit/solver"
require_relative "handover_audit/report"

# Reconstruction of which sealed view version and which material row every
# published row of a shared SQLite3::Statement cursor was actually read from,
# when parts of the handover log only record the permitted action combinations.
module HandoverAudit
  VERSION = "0.1.0"

  module_function

  def audit(spec)
    audit_case = Case.new(spec)
    Solver.new(audit_case).solve
  end
end
