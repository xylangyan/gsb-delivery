require 'geocoder/replay/analysis'
require 'geocoder/replay/cli'
require 'geocoder/replay/errors'
require 'geocoder/replay/scenario'

module Geocoder
  module Replay
    ##
    # Convenience facade used by the executable and by tests.
    ##
    module_function

    def load(path)
      Scenario.load(path)
    end

    def analyze(path_or_scenario, max_orders: Analysis::MAX_ORDERS, max_nodes: Analysis::MAX_NODES)
      scenario =
        path_or_scenario.is_a?(Scenario) ? path_or_scenario : Scenario.load(path_or_scenario)
      Analysis.new(scenario, max_orders: max_orders, max_nodes: max_nodes).run
    end
  end
end
