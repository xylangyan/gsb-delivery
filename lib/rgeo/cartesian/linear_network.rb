# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# Layer-aware, measure-enabled linear network segmentation
#
# -----------------------------------------------------------------------------

module RGeo
  module Cartesian
    # A LinearNetwork takes source linear features (routes) that carry their
    # own linear measure (the M coordinate), splits them at every node where
    # connectivity actually changes, and stores the result as directed
    # network links.
    #
    # The split points are:
    #
    # * every source route vertex (the only places where a route is allowed
    #   to begin or end a link), and
    # * the planar intersection of two routes on the same level, including
    #   both endpoints of a collinear overlap.
    #
    # Two routes only connect when they share a level (facility layer) and
    # the pair (or the whole level) is listed in the transfer rules. A
    # route on another level that crosses in the plane never transfers
    # onto the crossed routes. Routes on the same level that overlap or
    # cross without a transfer rule are still split at the shared
    # geometry, mirroring the ordinary Cartesian planar graph, but they
    # cannot be traversed into one another.
    #
    # Every stored link keeps the M interval of every source route that
    # supports it, so the original measure of any source can be recovered
    # from any link. M is interpolated linearly between the measures given
    # at the source vertices. Opposite digitized directions over the same
    # geometry stay separate links and keep each source's own M range.
    #
    # The network is versioned. Removing or replacing a source route
    # rebuilds one level at a time and only links whose supporting route
    # set changes get a new identity: links still supported by exactly the
    # same routes keep their id. Every retired link stays readable through
    # +retired_links+ together with the ids of the links that replaced it.
    class LinearNetwork
      # A directed link of the network. A link belongs to one level, runs
      # from +start_node+ to +end_node+ in a fixed digitized direction, and
      # records the closed M interval of every source route supporting it.
      class Link
        def initialize(id, level, start_node, end_node, route_ranges, generation,
                       factory, predecessor_ids = [])
          @id = id
          @level = level
          @start_node = start_node
          @end_node = end_node
          @route_ranges = route_ranges
          @generation = generation
          @factory = factory
          @predecessor_ids = predecessor_ids.dup
          @successor_ids = []
          @retired = false
        end

        attr_reader :id, :level, :start_node, :end_node, :route_ranges,
                    :generation, :predecessor_ids, :successor_ids
        attr_accessor :retired

        # @param predecessor_id [Object]
        # @return [void]
        def add_predecessor(predecessor_id)
          @predecessor_ids << predecessor_id unless @predecessor_ids.include?(predecessor_id)
        end

        # @return [RGeo::Feature::Point]
        def start_point
          @start_node.point
        end

        # @return [RGeo::Feature::Point]
        def end_point
          @end_node.point
        end

        # @return [RGeo::Feature::LineString]
        def geometry
          @factory.line_string([start_point, end_point])
        end

        # @return [Array<Object>] the source routes supporting this link
        def route_ids
          @route_ranges.keys
        end

        # Returns the closed M interval [from_m, to_m] that the given
        # source route covers on this link, ordered low to high regardless
        # of the route's digitized direction.
        #
        # @param route_id [Object]
        # @return [Array(Float, Float), nil]
        def m_range(route_id)
          @route_ranges[route_id]
        end

        # @param route_id [Object]
        # @return [Boolean]
        def supports?(route_id)
          @route_ranges.key?(route_id)
        end

        # @return [Boolean]
        def active?
          !@retired
        end

        def inspect
          "#<#{self.class.name} #{id} level=#{level} " \
            "#{start_node.coordinates}->#{end_node.coordinates} " \
            "routes=#{@route_ranges.keys.inspect}#{" retired" if @retired}>"
        end
      end

      # A network node. Nodes at the same coordinates are distinct per
      # level, so a planar crossing between different levels never joins
      # the two networks.
      class Node
        def initialize(coordinates, level, point)
          @coordinates = coordinates
          @level = level
          @point = point
        end

        attr_reader :coordinates, :level, :point

        def eql?(other)
          other.is_a?(Node) && other.level == level && other.coordinates == coordinates
        end
        alias == eql?

        def hash
          [level, coordinates].hash
        end
      end

      # @param factory [RGeo::Feature::Factory] Cartesian factory used to
      #   build nodes and link geometries. It does not need M support.
      def initialize(factory: nil)
        @factory = factory || Cartesian.simple_factory
        @level_route_sets = Hash.new { |hash, key| hash[key] = Set.new }
        @node_cache = Hash.new { |hash, key| hash[key] = {} }
        @transfer_pairs = Set.new
        @transfer_levels = Set.new
        @routes = {}
        @nodes = {}
        @links = {}
        @retired_links = {}
        @next_link_id = 1
        @generation = 0
      end

      attr_reader :routes, :nodes, :links, :retired_links, :generation

      # Declares that every pair of routes on +level+ may transfer onto
      # one another at shared nodes.
      #
      # @return [void]
      def allow_transfers_on(level)
        @transfer_levels << level
      end

      # Declares that the two given routes may transfer onto one another
      # at shared nodes. Both must still be on the same level.
      #
      # @return [void]
      def allow_transfer_between(route_id, other_route_id)
        @transfer_pairs << [route_id, other_route_id].sort
      end

      # Adds or replaces a source route and rebuilds the links on its
      # level.
      #
      # @param route_id [Object] identity of the source route
      # @param line_string [RGeo::Feature::LineString] line string whose
      #   points carry M; M is interpolated linearly along each segment
      # @param level [Object] facility layer of the route
      #
      # @return [void]
      def add_route(route_id, line_string, level)
        remove_route(route_id) if @routes.key?(route_id)

        @routes[route_id] = {
          line_string: line_string,
          level: level,
          vertices: build_vertices(line_string, level)
        }
        @level_route_sets[level] << route_id
        rebuild_level(level)
      end

      # Removes a source route. Links still supported by exactly the same
      # set of routes keep their identity; only links whose support
      # changes are retired and replaced.
      #
      # @param route_id [Object]
      #
      # @return [void]
      def remove_route(route_id)
        return unless @routes.key?(route_id)

        level = @routes.delete(route_id)[:level]
        @level_route_sets[level].delete(route_id)
        rebuild_level(level)
      end

      # @return [Array<Link>] the links currently in the network
      def active_links
        @links.values.reject(&:retired)
      end

      # Returns the active links traversed by the given source route whose
      # M interval overlaps the given range, ordered along the route.
      #
      # This answers "which small segments does this original route pass
      # through between these measures".
      #
      # @param route_id [Object]
      # @param from_m [Numeric, nil] inclusive lower M bound; nil means
      #   the start of the route
      # @param to_m [Numeric, nil] inclusive upper M bound; nil means the
      #   end of the route
      #
      # @return [Array<Link>]
      def links_for_route(route_id, from_m: nil, to_m: nil)
        return [] unless @routes.key?(route_id)

        links = active_links.select { |link| link.supports?(route_id) }
        links = links.select do |link|
          low, high = link.m_range(route_id)
          (from_m.nil? || high >= from_m) && (to_m.nil? || low <= to_m)
        end
        sort_links_along_route(links, route_id)
      end

      # Returns the routes reachable from the given route at a shared
      # node, following only the declared transfer rules.
      #
      # @param route_id [Object]
      # @param at [Array(Float, Float), RGeo::Feature::Point, nil]
      #   restrict the answer to the node at these coordinates; nil means
      #   any shared node
      # @param include_self [Boolean] include the route itself
      #
      # @return [Array<Object>]
      def transfers_from(route_id, at: nil, include_self: false)
        return [] unless @routes.key?(route_id)

        level = @routes[route_id][:level]
        coords = planar_coordinates(at)
        @level_route_sets[level].to_a.select do |other_id|
          next false if other_id == route_id && !include_self
          next false unless transfer_allowed?(route_id, other_id, level)
          next true if coords.nil?

          node_used_by?(route_id, coords) && node_used_by?(other_id, coords)
        end
      end

      # Returns the active links that may be entered when leaving the
      # given link through one of its end nodes, honoring direction and
      # transfer rules.
      #
      # @param link [Link]
      # @param through [Symbol] +:end_node+ (default) or +:start_node+
      #
      # @return [Array<Link>]
      def connections(link, through: :end_node)
        node = through == :start_node ? link.start_node : link.end_node
        active_links.select do |other|
          next false if other.equal?(link)
          next false unless other.start_node == node
          next false unless other.level == link.level

          link.route_ids.any? do |route_id|
            other.route_ids.any? do |other_route_id|
              transfer_allowed?(route_id, other_route_id, link.level)
            end
          end
        end
      end

      private

      # ---- Route ingestion -------------------------------------------------

      def build_vertices(line_string, level)
        unless line_string.is_a?(Feature::LineString)
          raise Error::RGeoError, "route must be a LineString"
        end
        if line_string.num_points < 2
          raise Error::RGeoError, "route needs at least two vertices"
        end

        line_string.points.map do |point|
          unless point.respond_to?(:m) && !point.m.nil?
            raise Error::RGeoError, "every route vertex must carry an M value"
          end

          {
            coordinates: [point.x, point.y],
            level: level,
            point: point,
            m: point.m.to_f
          }
        end
      end

      # ---- Level rebuild ---------------------------------------------------

      def rebuild_level(level)
        @generation += 1
        route_ids = @level_route_sets[level].to_a
        records = build_level_records(level, route_ids)
        replace_links(level, records)
      end

      # Produces the target link records for one level. A record carries
      # directed geometry and the M interval contributed by each route.
      def build_level_records(level, route_ids)
        route_ids.each do |route_id|
          @routes[route_id][:vertices].each { |vertex| node_for(level, vertex[:coordinates]) }
        end

        cut_points = collect_cut_points(route_ids)
        pieces = []

        route_ids.each do |route_id|
          vertices = @routes[route_id][:vertices]
          each_route_piece(vertices, cut_points[route_id]) do |start_coords, end_coords, start_m, end_m|
            node_for(level, start_coords)
            node_for(level, end_coords)
            pieces << {
              start_coords: start_coords,
              end_coords: end_coords,
              route_id: route_id,
              range: [start_m, end_m].minmax
            }
          end
        end

        nodes_by_coords = @node_cache[level]
        merge_pieces(pieces, nodes_by_coords)
      end

      # Every coordinates at which a route must be split: its own vertices
      # plus every planar intersection with another route on the same
      # level. A collinear overlap contributes both of its endpoints.
      def collect_cut_points(route_ids)
        cut_points = Hash.new { |hash, key| hash[key] = Set.new }
        route_ids.each do |route_id|
          @routes[route_id][:vertices].each do |vertex|
            cut_points[route_id] << vertex[:coordinates]
          end
        end

        route_ids.combination(2) do |route_id, other_id|
          # Planar intersections always split the routes on their own
          # level. Transfer rules only decide whether the resulting node
          # can be used to move between routes.
          each_intersection(route_id, other_id) do |coords|
            cut_points[route_id] << coords
            cut_points[other_id] << coords
          end
        end
        cut_points
      end

      def each_intersection(route_id, other_id)
        return to_enum(__method__, route_id, other_id) unless block_given?

        each_segment(route_id) do |seg_one|
          each_segment(other_id) do |seg_two|
            intersection_coordinates(seg_one, seg_two).each { |coords| yield coords }
          end
        end
      end

      def each_segment(route_id)
        return to_enum(__method__, route_id) unless block_given?

        @routes[route_id][:vertices].each_cons(2) do |start_vertex, end_vertex|
          yield Segment.new(start_vertex[:point], end_vertex[:point])
        end
      end

      # Distinct intersection coordinates of two segments. A collinear
      # overlap yields its two endpoints; a crossing yields one point.
      def intersection_coordinates(seg_one, seg_two)
        coords = [seg_one.s, seg_one.e, seg_two.s, seg_two.e].filter_map do |point|
          [point.x, point.y] if seg_one.contains_point?(point) && seg_two.contains_point?(point)
        end

        if seg_one.dx * seg_two.dy - seg_one.dy * seg_two.dx != 0
          point = seg_one.segment_intersection(seg_two)
          coords << [point.x, point.y] if point
        end
        coords.uniq
      end

      # Walks one route, cutting each source segment at the given cut
      # points, and yields every piece as [start_coords, end_coords,
      # start_m, end_m] with M linearly interpolated at both ends.
      def each_route_piece(vertices, route_cut_points)
        return to_enum(__method__, vertices, route_cut_points) unless block_given?

        vertices.each_cons(2) do |start_vertex, end_vertex|
          segment = Segment.new(start_vertex[:point], end_vertex[:point])
          coords = route_cut_points.select { |point_coords| on_segment?(point_coords, segment) }
          coords |= [start_vertex[:coordinates], end_vertex[:coordinates]]
          coords = sort_coordinates_along(coords, segment)

          coords.each_cons(2) do |from_coords, to_coords|
            next if from_coords == to_coords

            yield [
              from_coords,
              to_coords,
              measure_at(start_vertex, end_vertex, from_coords),
              measure_at(start_vertex, end_vertex, to_coords)
            ]
          end
        end
      end

      def sort_coordinates_along(coords, segment)
        coords.sort_by do |point_coords|
          point = segment.s.factory.point(point_coords[0], point_coords[1])
          projection = segment.tproj(point) || 0.0
          [projection, point_coords]
        end
      end

      def measure_at(start_vertex, end_vertex, coords)
        return start_vertex[:m] if coords == start_vertex[:coordinates]
        return end_vertex[:m] if coords == end_vertex[:coordinates]

        segment = Segment.new(start_vertex[:point], end_vertex[:point])
        point = segment.s.factory.point(coords[0], coords[1])
        t = segment.tproj(point)
        start_vertex[:m] + t * (end_vertex[:m] - start_vertex[:m])
      end

      def on_segment?(coords, segment)
        point = segment.s.factory.point(coords[0], coords[1])
        segment.contains_point?(point)
      end

      # Accepts either an [x, y] array or a point (possibly carrying M).
      def planar_coordinates(location)
        location.respond_to?(:x) ? [location.x, location.y] : location
      end

      # Merges pieces covering the same directed geometry, combining the
      # M ranges of their supporting routes.
      def merge_pieces(pieces, nodes_by_coords)
        grouped = {}
        pieces.each do |piece|
          key = [piece[:start_coords], piece[:end_coords]]
          grouped[key] ||= {
            start_coords: piece[:start_coords],
            end_coords: piece[:end_coords],
            route_ranges: {}
          }
          grouped[key][:route_ranges][piece[:route_id]] = piece[:range]
        end

        grouped.values.map do |record|
          record[:start_node] = nodes_by_coords[record[:start_coords]]
          record[:end_node] = nodes_by_coords[record[:end_coords]]
          record
        end
      end

      # ---- Identity-preserving replacement ---------------------------------

      def replace_links(level, new_records)
        old_links = active_links.select { |link| link.level == level }
        old_by_key = {}
        old_links.each { |link| old_by_key[link_key(link)] = link }

        kept = Set.new
        created = []
        new_records.each do |record|
          existing = old_by_key[record_key(record)]
          if existing && existing.route_ranges == record[:route_ranges]
            kept << existing.id
          else
            created << record
          end
        end

        retired = old_links.reject { |link| kept.include?(link.id) }
        retired.each do |link|
          link.retired = true
          @retired_links[link.id] = link
          @links.delete(link.id)
        end

        created.each do |record|
          predecessors = predecessor_links_for(record, old_links)
          link = Link.new(
            next_link_id,
            level,
            record[:start_node],
            record[:end_node],
            record[:route_ranges],
            @generation,
            @factory,
            predecessors.map(&:id)
          )
          predecessors.each { |old_link| old_link.successor_ids << link.id }
          @links[link.id] = link
        end

        # A retired link can be replaced by a link that no longer shares
        # any route with it (for example, the only supporting route was
        # removed). Point it at every surviving link on the same geometry.
        retired.each do |link|
          active_links.each do |candidate|
            next unless candidate.level == level
            next unless links_share_geometry?(link, candidate)
            next if link.successor_ids.include?(candidate.id)

            link.successor_ids << candidate.id
            candidate.add_predecessor(link.id)
          end
        end

        rebuild_nodes(level)
      end

      # Retired links are traced to the replacements covering the same
      # geometry, in either direction. A route that disappears can leave a
      # common link supported by another route, which then has no route in
      # common with the retired link.
      def predecessor_links_for(record, old_links)
        old_links.select { |link| same_geometry?(link, record[:start_coords], record[:end_coords]) }
      end

      def links_share_geometry?(link, other)
        same_geometry?(link, other.start_node.coordinates, other.end_node.coordinates)
      end

      def same_geometry?(link, start_coords, end_coords)
        (link.start_node.coordinates == start_coords &&
          link.end_node.coordinates == end_coords) ||
          (link.start_node.coordinates == end_coords &&
            link.end_node.coordinates == start_coords)
      end

      def link_key(link)
        [link.start_node.coordinates, link.end_node.coordinates, link.route_ranges.keys.sort]
      end

      def record_key(record)
        [record[:start_coords], record[:end_coords], record[:route_ranges].keys.sort]
      end

      def rebuild_nodes(level)
        @nodes.delete_if { |key, _| key.first == level }
        active_links.each do |link|
          next unless link.level == level

          @nodes[[level, link.start_node.coordinates]] = link.start_node
          @nodes[[level, link.end_node.coordinates]] = link.end_node
        end
      end

      # Nodes keep their identity across rebuilds as long as their level
      # and coordinates survive, so external references stay valid.
      def node_for(level, coords)
        @node_cache[level][coords] ||=
          Node.new(coords, level, @factory.point(coords[0], coords[1]))
      end

      def next_link_id
        id = @next_link_id
        @next_link_id += 1
        id
      end

      # ---- Queries ---------------------------------------------------------

      # A route always continues onto itself; distinct routes need an
      # explicit level-wide or pair-specific transfer rule.
      def transfer_allowed?(route_id, other_id, level)
        return true if route_id == other_id
        return true if @transfer_levels.include?(level)

        @transfer_pairs.include?([route_id, other_id].sort)
      end

      def node_used_by?(route_id, coords)
        active_links.any? do |link|
          link.supports?(route_id) &&
            (link.start_node.coordinates == coords || link.end_node.coordinates == coords)
        end
      end

      # Orders links by their geometric position along the digitized
      # direction of the source route, so a decreasing M scale does not
      # reverse the answer.
      def sort_links_along_route(links, route_id)
        vertices = @routes[route_id][:vertices]
        links.sort_by do |link|
          start_position = route_position(vertices, link.start_node.coordinates)
          end_position = route_position(vertices, link.end_node.coordinates)
          [start_position, end_position].min
        end
      end

      # Scalar position of coordinates along a route: the source segment
      # index plus the projection inside that segment.
      def route_position(vertices, coords)
        vertices.each_cons(2).with_index do |(start_vertex, end_vertex), index|
          segment = Segment.new(start_vertex[:point], end_vertex[:point])
          next unless on_segment?(coords, segment)

          point = segment.s.factory.point(coords[0], coords[1])
          return index + (segment.tproj(point) || 0.0)
        end
        Float::INFINITY
      end
    end
  end
end
