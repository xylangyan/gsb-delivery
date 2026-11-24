# frozen_string_literal: true

#--
# BibTeX-Ruby multi-institution merge.
#
# Input: a handover manifest (JSON) and one BibTeX file per institution.
# Output: one BibTeX file that
#
#   * reopens with BibTeX.open,
#   * expands with Bibliography#replace and Bibliography#join, and
#   * answers per-institution queries through Entry#get, aliases included.
#
# Rules enforced here:
#
#   * only macro names and parent records named in the manifest are used;
#   * a formal macro keeps at most one institution's original @string, copied
#     verbatim, so references inside it stay exactly as written;
#   * a formal conference keeps at most one institution's parent record, also
#     copied verbatim; a parent whose expansion would change is not eligible;
#   * a child either keeps a crossref to the kept parent or drops it and
#     carries every value it used to inherit as an explicit field;
#   * a child's explicit field keeps its original expression when that still
#     expands to the same value, and otherwise becomes that exact value;
#   * dropping a crossref counts as one changed child field, as do adding and
#     replacing one.
#
# The solver minimises changed child fields, then follows the candidate order
# in the manifest. Macro and conference groups that share no formal name are
# solved independently, so unrelated additions do not multiply the search.
#++

require 'json'
require 'optparse'
require 'set'

$LOAD_PATH.unshift(File.expand_path('../lib', __dir__))
require 'bibtex'

module BibMerge
  ALIASES = BibTeX::Entry::FIELD_ALIASES

  # Names the library would answer through an alias for the fields present.
  def self.alias_names(fields)
    ALIASES.select { |_alias_name, target| fields.key?(target) }.keys
  end

  # Raised when the inputs cannot reproduce every institution's view.
  class UnrepresentableError < StandardError
    attr_reader :conflicts

    def initialize(message, conflicts = [])
      super(message)
      @conflicts = conflicts
    end
  end

  # An expanded field: +tokens+ is the expression as written, +value+ the
  # exact string after local macro expansion and joining.
  Form = Struct.new(:tokens, :value) do
    def literal?
      tokens.none? { |token| token.is_a?(Symbol) }
    end

    def expression
      tokens.map { |token| token.is_a?(Symbol) ? token.to_s : token.inspect }.join(' # ')
    end

    def literal
      Form.new([value], value)
    end
  end

  # One institution's parsed and expanded bibliography.
  class Source
    attr_reader :id, :path, :raw, :expanded

    def initialize(id, path)
      @id = id
      @path = path
      text = File.read(path, encoding: 'utf-8')
      @raw = BibTeX.parse(text, parse_names: false, parse_months: false)
      @expanded = BibTeX.parse(text, parse_names: false, parse_months: false)
      @expanded.replace
      @expanded.join
    end

    def entry(key, expanded: false)
      (expanded ? @expanded : @raw).entries[key.to_s]
    end

    def string(key, expanded: false)
      (expanded ? @expanded : @raw).strings[key.to_sym]
    end

    def form(entry, name)
      raw = entry.fields[name]
      return nil if raw.nil?

      expanded_entry = @expanded.entries[entry.key]
      value = expanded_entry&.fields&.[](name)
      Form.new(raw.tokens.dup, value&.to_s)
    end

    def forms(entry)
      entry.fields.to_h { |name, _| [name, form(entry, name)] }
    end
  end

  # A candidate @string definition, in manifest order.
  MacroCandidate = Struct.new(:institution, :key, :form, keyword_init: true)

  # A candidate parent record, in manifest order.
  ParentCandidate = Struct.new(:institution, :key, :entry, :forms, keyword_init: true)

  # One child record and the view its institution has of it.
  class Child
    attr_reader :institution, :local_key, :formal_key, :entry, :type,
                :explicit, :inherited

    def initialize(institution:, local_key:, formal_key:, entry:, type:,
                   explicit:, inherited:)
      @institution = institution
      @local_key = local_key
      @formal_key = formal_key
      @entry = entry
      @type = type
      @explicit = explicit
      @inherited = inherited
    end

    def had_crossref?
      !@entry.fields[:crossref].nil?
    end

    # Names this child can answer, including names inherited through the
    # library's alias rule. The alias applies to the parent record only: a
    # child's own title does not become its booktitle.
    def names
      (explicit.keys + inherited.keys).uniq
    end

    # The value this institution sees for +name+. Alias lookup applies only
    # to the parent record; a child's own title does not become its
    # booktitle.
    def [](name)
      name = name.to_sym
      if (form = explicit[name])
        form.value
      elsif (form = inherited[name])
        form.value
      elsif (alias_name = ALIASES[name]) && (form = inherited[alias_name])
        form.value
      end
    end

    def view(extra_names = [])
      names = (self.names + extra_names + alias_names).uniq
      names.to_h { |name| [name, self[name]] }
    end

    def alias_names
      BibMerge.alias_names(inherited)
    end
  end

  # One formal conference and its candidates/children.
  class Conference
    attr_reader :formal, :parents, :children

    def initialize(formal)
      @formal = formal
      @parents = []
      @children = []
    end
  end

  # A macro under its formal name, with candidates in manifest order.
  class Macro
    attr_reader :formal, :candidates

    def initialize(formal)
      @formal = formal
      @candidates = []
    end
  end

  # Result of evaluating one conference under one macro assignment.
  Plan = Struct.new(
    :conference, :parent_index, :parent_forms, :children, :cost, keyword_init: true
  )

  # Result of evaluating one child.
  ChildPlan = Struct.new(
    :child, :crossref, :explicit, :changes, :cost, keyword_init: true
  )

  class Merger
    attr_reader :bibliography, :report

    def initialize(manifest, root: Dir.pwd)
      @manifest = manifest
      @root = root
      @institutions = manifest.fetch('institutions')
      @sources = {}
      @macros = {}
      @conferences = {}
      @citations = []
      @macro_choice = {}
      @plans = {}
      @citation_plans = []
      @conflicts = []
      @component_groups = []
    end

    def run
      load_sources
      load_macros
      load_conferences
      load_citations
      check_formal_keys
      solve
      @bibliography = build_bibliography
      verify
      self
    end

    def to_bibtex
      @bibliography.to_s
    end

    def output_path=(path)
      @output_path = path
    end

    private

    def path_for(relative)
      File.expand_path(relative, @root)
    end

    def load_sources
      @institutions.each do |institution|
        id = institution.fetch('id')
        @sources[id] = Source.new(id, path_for(institution.fetch('file')))
      end
    end

    def load_macros
      seen = Set.new
      @manifest.fetch('macros').each do |spec|
        formal = spec.fetch('formal')
        raise ArgumentError, "duplicate formal macro #{formal}" unless seen.add?(formal)

        macro = Macro.new(formal)
        spec.fetch('candidates').each do |candidate|
          source = @sources.fetch(candidate.fetch('institution'))
          key = candidate.fetch('key')
          string = source.string(key)
          raise ArgumentError, "#{candidate['institution']}: no @string #{key}" if string.nil?

          form = Form.new(string.value.tokens.dup,
                          source.string(key, expanded: true)&.value&.to_s)
          macro.candidates << MacroCandidate.new(
            institution: candidate['institution'], key: key.to_sym, form: form
          )
        end
        @macros[formal] = macro
      end
    end

    def load_conferences
      seen = Set.new
      @manifest.fetch('conferences').each do |spec|
        formal = spec.fetch('formal')
        raise ArgumentError, "duplicate formal conference #{formal}" unless seen.add?(formal)

        conference = Conference.new(formal)
        spec.fetch('parents').each do |candidate|
          source = @sources.fetch(candidate.fetch('institution'))
          entry = source.entry(candidate.fetch('key'))
          raise ArgumentError, "#{candidate['institution']}: no entry #{candidate['key']}" if entry.nil?

          conference.parents << ParentCandidate.new(
            institution: candidate['institution'], key: candidate['key'],
            entry: entry, forms: source.forms(entry)
          )
        end

        spec.fetch('children').each do |candidate|
          source = @sources.fetch(candidate.fetch('institution'))
          local_key = candidate.fetch('key')
          entry = source.entry(local_key)
          raise ArgumentError, "#{candidate['institution']}: no entry #{local_key}" if entry.nil?

          explicit = source.forms(entry)
          explicit.delete(:crossref)
          conference.children << Child.new(
            institution: candidate['institution'], local_key: local_key,
            formal_key: candidate.fetch('formal'), entry: entry, type: entry.type,
            explicit: explicit, inherited: inherited_forms(source, entry)
          )
        end

        @conferences[formal] = conference
      end
    end

    # Values the entry actually inherits through its original crossref,
    # following the library's alias rule.
    def inherited_forms(source, entry)
      parent = entry.parent
      return {} if parent.nil?

      expanded_parent = source.expanded.entries[parent.key]
      names = parent.fields.keys.dup
      names += ALIASES.select { |_alias_name, target| parent.fields.key?(target) }.keys
      names.uniq.to_h do |name|
        raw = parent.fields[name] ||
              (ALIASES[name] && parent.fields[ALIASES[name]])
        expanded = expanded_parent&.fields&.[](name) ||
                   (ALIASES[name] && expanded_parent&.fields&.[](ALIASES[name]))
        [name, Form.new(raw.tokens.dup, expanded&.to_s)]
      end.reject { |name, _| entry.fields.key?(name) }
    end

    def load_citations
      @manifest.fetch('citations', []).each do |spec|
        source = @sources.fetch(spec.fetch('institution'))
        local_key = spec.fetch('key')
        entry = source.entry(local_key)
        raise ArgumentError, "#{spec['institution']}: no entry #{local_key}" if entry.nil?

        explicit = source.forms(entry)
        explicit.delete(:crossref)
        @citations << Child.new(
          institution: spec['institution'], local_key: local_key,
          formal_key: spec.fetch('formal'), entry: entry, type: entry.type,
          explicit: explicit, inherited: inherited_forms(source, entry)
        )
      end
    end

    def check_formal_keys
      groups = Hash.new { |h, k| h[k] = [] }
      (@conferences.values.flat_map(&:children) + @citations).each do |child|
        groups[child.formal_key] << child
      end

      groups.each do |formal, children|
        next if children.length == 1

        first = children.first
        if children.all? { |child| child.view == first.view }
          # Identical records: one output entry satisfies every view, but all
          # institutions must still be verified against it.
          @duplicates ||= []
          children.drop(1).each { |child| @duplicates << child }
          @conferences.each_value do |conference|
            conference.children.reject! { |child| @duplicates.include?(child) }
          end
          @citations.reject! { |child| @duplicates.include?(child) }
          next
        end

        other = children.find { |child| child.view != first.view }
        union = (first.view.keys + other.view.keys).uniq
        field = union.find { |name| first[name] != other[name] }
        @conflicts << {
          'kind' => 'formal_key_collision',
          'formal_key' => formal,
          'field' => field.to_s,
          'extra_institution' => first[field].nil? ? other.institution : first.institution,
          'missing_institution' => first[field].nil? ? first.institution : other.institution,
          'values' => {
            first.institution => first[field],
            other.institution => other[field]
          },
          'detail' => "field #{field} cannot be both present and absent under #{formal}"
        }
      end

      @conferences.each_key do |formal|
        next unless groups[formal]
        next if groups[formal].all? do |child|
          @conferences[formal].children.any? { |c| c.equal?(child) }
        end

        @conflicts << {
          'kind' => 'citation_conference_key_collision',
          'formal_key' => formal,
          'field' => nil,
          'detail' => 'the same formal name is used for a citation and a conference'
        }
      end

      return if @conflicts.empty?

      raise UnrepresentableError.new(
        'the handover cannot be represented as one equivalent bibliography',
        @conflicts
      )
    end

    # ------------------------------------------------------------- solving

    def solve
      @component_groups = components
      @component_groups.each { |component| solve_component(component) }
    end

    # Connected groups of macros and conferences. Two nodes are connected
    # when one refers to the other's formal name.
    def components
      parent = {}
      find = lambda do |node|
        parent[node] ||= node
        parent[node] = find.call(parent[node]) if parent[node] != node
        parent[node]
      end
      union = ->(a, b) { parent[find.call(a)] = find.call(b) }

      @macros.each_key { |name| find.call([:macro, name]) }
      @conferences.each_key { |name| find.call([:conference, name]) }

      @macros.each do |name, macro|
        macro.candidates.each do |candidate|
          symbols(candidate.form).each do |symbol|
            union.call([:macro, name], [:macro, symbol]) if @macros.key?(symbol)
          end
        end
      end

      @conferences.each do |name, conference|
        forms = conference.parents.flat_map { |candidate| candidate.forms.values }
        forms += conference.children.flat_map { |child| child.explicit.values }
        forms.each do |form|
          symbols(form).each do |symbol|
            union.call([:conference, name], [:macro, symbol]) if @macros.key?(symbol)
          end
        end
      end

      @citations.each do |child|
        find.call([:conference, :citations])
        child.explicit.each_value do |form|
          symbols(form).each do |symbol|
            union.call([:conference, :citations], [:macro, symbol]) if @macros.key?(symbol)
          end
        end
      end

      groups = Hash.new { |h, k| h[k] = { macros: [], conferences: [], citations: [] } }
      @macros.each_key { |name| groups[find.call([:macro, name])][:macros] << name }
      @conferences.each_key do |name|
        groups[find.call([:conference, name])][:conferences] << name
      end
      unless @citations.empty?
        groups[find.call([:conference, :citations])][:citations] = @citations
      end
      groups.values.map do |group|
        {
          macros: group[:macros].sort_by { |n| macro_order(n) },
          conferences: group[:conferences].sort_by { |n| conference_order(n) },
          citations: group[:citations]
        }
      end
    end

    def symbols(form)
      form.tokens.grep(Symbol).map(&:to_s).uniq
    end

    def solve_component(component)
      macro_names = component[:macros]
      conference_names = component[:conferences]

      best = nil
      macro_assignments(macro_names).each do |assignment|
        plans = conference_names.to_h do |name|
          [name, best_plan(@conferences.fetch(name), assignment)]
        end
        next if plans.values.any?(&:nil?)

        citation_plans = component[:citations].map { |child| plan_standalone(child, assignment) }
        next if citation_plans.any?(&:nil?)

        candidate = {
          assignment: assignment, plans: plans, citations: citation_plans
        }
        best = candidate if best.nil? || (rank(candidate) <=> rank(best)).negative?
      end

      unless best
        diagnose_component(component)
        return
      end

      best[:assignment].each { |name, index| @macro_choice[name] = index }
      best[:plans].each { |name, plan| @plans[name] = plan }
      best[:citations].each { |plan| @citation_plans << plan }
    end

    # Lower is better: cost first, then parent and macro candidates in the
    # order the handover lists them.
    def rank(candidate)
      vector = [candidate[:plans].values.sum(&:cost) +
                candidate[:citations].sum(&:cost)]
      @conferences.each_key do |name|
        plan = candidate[:plans][name]
        next if plan.nil?

        vector << (plan.parent_index.nil? ? 10_000 : plan.parent_index)
      end
      @macros.each_key do |name|
        next unless candidate[:assignment].key?(name)

        index = candidate[:assignment][name]
        vector << (index.nil? ? 10_000 : index)
      end
      vector
    end

    def macro_order(name)
      @manifest.fetch('macros').index { |spec| spec['formal'] == name } || 10_000
    end

    def conference_order(name)
      @manifest.fetch('conferences').index { |spec| spec['formal'] == name } || 10_000
    end

    def macro_assignments(names)
      return [{}] if names.empty?

      name = names.first
      rest = macro_assignments(names.drop(1))
      options = [nil] + (0...@macros.fetch(name).candidates.length).to_a
      options.flat_map do |index|
        rest.map { |assignment| assignment.merge(name => index) }
      end
    end

    # Expansion of every kept macro under +assignment+; nil when a kept
    # definition refers to a macro that is not kept, or cycles.
    def expansions(assignment)
      @expansion_cache ||= {}
      cache_key = assignment.sort_by { |name, _| name }
      return @expansion_cache[cache_key] if @expansion_cache.key?(cache_key)

      resolved = {}
      assignment.each do |name, index|
        next if index.nil?

        value = expand_macro(name, assignment, resolved, [])
        if value.nil?
          @expansion_cache[cache_key] = nil
          return nil
        end
        resolved[name] = value
      end
      @expansion_cache[cache_key] = resolved
    end

    def expand_macro(name, assignment, resolved, stack)
      return resolved[name] if resolved.key?(name)
      return nil if stack.include?(name)

      index = assignment[name]
      return nil if index.nil?

      form = @macros.fetch(name).candidates.fetch(index).form
      out = +''
      form.tokens.each do |token|
        if token.is_a?(Symbol)
          value = expand_macro(token.to_s, assignment, resolved, stack + [name])
          return nil if value.nil?

          out << value
        else
          out << token.to_s
        end
      end
      resolved[name] = out
    end

    def best_plan(conference, assignment)
      expanded = expansions(assignment)
      return nil if expanded.nil?

      options = [nil] + (0...conference.parents.length).to_a
      plans = options.filter_map do |index|
        plan_conference(conference, index, assignment, expanded)
      end
      plans.min_by { |plan| [plan.cost, plan.parent_index.nil? ? 10_000 : plan.parent_index] }
    end

    def plan_conference(conference, parent_index, assignment, expanded)
      parent = parent_index.nil? ? nil : conference.parents[parent_index]
      parent_forms =
        if parent
          parent.forms.to_h do |name, form|
            [name, verbatim(form, assignment, expanded)]
          end
        else
          {}
        end
      return nil if parent_forms.values.any?(&:nil?)

      child_plans = conference.children.map do |child|
        plan_child(child, conference, parent, parent_forms, assignment, expanded)
      end
      return nil if child_plans.any?(&:nil?)

      Plan.new(
        conference: conference, parent_index: parent_index,
        parent_forms: parent_forms, children: child_plans,
        cost: child_plans.sum(&:cost)
      )
    end

    # A parent or macro form may only be copied exactly: every symbol must be
    # kept and the expansion must equal the source value. Otherwise the
    # candidate would have to invent a new value.
    def verbatim(form, assignment, expanded)
      return form if form.literal?

      kept = form.tokens.all? { |token| !token.is_a?(Symbol) || expanded.key?(token.to_s) }
      return nil unless kept

      value = form.tokens.map do |token|
        token.is_a?(Symbol) ? expanded.fetch(token.to_s) : token.to_s
      end.join
      value == form.value ? form : nil
    end

    # A child's explicit field may keep its expression or fall back to the
    # exact expanded value.
    def writable(form, assignment, expanded)
      return form if form.literal?
      return nil if form.value.nil?

      kept = form.tokens.all? { |token| !token.is_a?(Symbol) || expanded.key?(token.to_s) }
      if kept
        value = form.tokens.map do |token|
          token.is_a?(Symbol) ? expanded.fetch(token.to_s) : token.to_s
        end.join
        return form if value == form.value
      end

      form.literal
    end

    def plan_child(child, conference, parent, parent_forms, assignment, expanded)
      explicit = {}
      changes = []
      cost = 0

      child.explicit.each do |name, form|
        written = writable(form, assignment, expanded)
        return nil if written.nil?

        explicit[name] = written
        next if written.tokens == form.tokens

        changes << change(child, 'replace', name, form, written.value)
        cost += 1
      end

      # Option A: no crossref. Write every value the child used to inherit.
      drop_explicit = explicit.dup
      drop_changes = changes.dup
      drop_cost = cost
      if child.had_crossref?
        drop_cost += 1
        drop_changes << change(child, 'delete_crossref', :crossref, nil,
                               child.entry.fields[:crossref]&.to_s)
      end
      child.inherited.each do |name, form|
        next if drop_explicit.key?(name) || form.value.nil?

        drop_explicit[name] = Form.new([form.value], form.value)
        drop_cost += 1
        drop_changes << change(child, 'add_inherited', name, form, form.value)
      end

      options = [
        [drop_cost, 1,
         ChildPlan.new(child: child, crossref: nil, explicit: drop_explicit,
                       changes: drop_changes, cost: drop_cost)]
      ]

      # Option B: keep the crossref. Only values the kept parent cannot
      # supply are written; a value the parent supplies but the institution
      # lacked makes this parent unusable.
      if parent && child.had_crossref?
        keep = explicit.dup
        keep_changes = changes.dup
        keep_cost = cost
        feasible = true

        names = (child.view.keys + parent_forms.keys +
                 BibMerge.alias_names(parent_forms)).uniq
        names.each do |name|
          expected = child[name]
          actual = effective_value(keep, parent_forms, name)
          next if expected == actual

          if expected.nil? || keep.key?(name)
            feasible = false
            break
          end

          keep[name] = Form.new([expected], expected)
          keep_cost += 1
          keep_changes << change(child, 'add_override', name, nil, expected)
        end

        if feasible
          options << [keep_cost, 0,
                      ChildPlan.new(child: child, crossref: conference.formal,
                                    explicit: keep, changes: keep_changes,
                                    cost: keep_cost)]
        end
      end

      options.min_by { |cost_value, preference, _plan| [cost_value, preference] }[2]
    end

    # The value an entry with +explicit+ fields would answer for +name+ with
    # +parent_forms+ as its crossref target, following the library's rules.
    def effective_value(explicit, parent_forms, name)
      name = name.to_sym
      if (form = explicit[name])
        form.value
      elsif (form = parent_forms[name])
        form.value
      elsif (alias_name = ALIASES[name]) && (form = parent_forms[alias_name])
        form.value
      end
    end

    def change(child, action, field, from, to)
      {
        'action' => action,
        'field' => field.to_s,
        'from' => from&.expression,
        'to' => to,
        'formal_key' => child.formal_key,
        'institution' => child.institution,
        'original_key' => child.local_key
      }
    end

    def diagnose_component(component)
      component[:macros].each do |name|
        @macros.fetch(name).candidates.each do |candidate|
          symbols(candidate.form).each do |symbol|
            next if @macros.key?(symbol)

            @conflicts << {
              'kind' => 'unknown_macro_reference',
              'formal_macro' => name,
              'institution' => candidate.institution,
              'original_key' => candidate.key.to_s,
              'field' => symbol,
              'detail' => "#{symbol} is referenced but is not a formal macro"
            }
          end
        end
      end

      component[:conferences].each do |name|
        conference = @conferences.fetch(name)
        conference.children.each do |child|
          fields = child.explicit.filter_map do |field, form|
            form.value.nil? ? field.to_s : nil
          end
          next if fields.empty?

          @conflicts << {
            'kind' => 'unresolved_field',
            'formal_key' => child.formal_key,
            'institution' => child.institution,
            'original_key' => child.local_key,
            'field' => fields.first,
            'fields' => fields,
            'detail' => 'the local source does not expand this field, so no exact value exists'
          }
        end
      end

      @conflicts << {
        'kind' => 'no_equivalent_plan',
        'formal_key' => component[:conferences].first,
        'field' => nil,
        'detail' => 'no macro/parent choice reproduces every institution view'
      } if @conflicts.empty?

      raise UnrepresentableError.new(
        'no equivalent merge for this group', @conflicts
      )
    end

    # -------------------------------------------------------------- output

    def build_bibliography
      bib = BibTeX::Bibliography.new(parse_names: false, parse_months: false)

      @manifest.fetch('macros').each do |spec|
        formal = spec['formal']
        index = @macro_choice[formal]
        next if index.nil?

        candidate = @macros.fetch(formal).candidates.fetch(index)
        bib << BibTeX::String.new(formal, BibTeX::Value.new(candidate.form.tokens.map(&:dup)))
      end

      @manifest.fetch('conferences').each do |spec|
        plan = @plans[spec['formal']]
        next if plan.nil?

        if plan.parent_index
          parent = plan.conference.parents.fetch(plan.parent_index)
          entry = BibTeX::Entry.new(bibtex_type: parent.entry.type, bibtex_key: spec['formal'])
          plan.parent_forms.each do |name, form|
            entry.add(name, BibTeX::Value.new(form.tokens.map(&:dup)))
          end
          bib << entry
        end

        plan.children.each do |child_plan|
          child = child_plan.child
          entry = BibTeX::Entry.new(bibtex_type: child.type, bibtex_key: child.formal_key)
          entry.add(:crossref, child_plan.crossref) if child_plan.crossref
          child_plan.explicit.each do |name, form|
            entry.add(name, BibTeX::Value.new(form.tokens.map(&:dup)))
          end
          bib << entry
        end
      end

      @citations.each do |child|
        plan = @citation_plans.find { |p| p.child.equal?(child) } ||
               plan_standalone(child, @macro_choice)
        entry = BibTeX::Entry.new(bibtex_type: child.type, bibtex_key: child.formal_key)
        plan.explicit.each do |name, form|
          entry.add(name, BibTeX::Value.new(form.tokens.map(&:dup)))
        end
        bib << entry
        @citation_plans << plan unless @citation_plans.include?(plan)
      end
      bib
    end

    def plan_standalone(child, assignment = nil)
      assignment ||= @macro_choice
      expanded = expansions(assignment) || {}
      explicit = {}
      changes = []
      cost = 0
      child.explicit.each do |name, form|
        written = writable(form, assignment, expanded)
        return nil if written.nil?

        explicit[name] = written
        next if written.tokens == form.tokens

        changes << change(child, 'replace', name, form, written.value)
        cost += 1
      end
      ChildPlan.new(child: child, crossref: nil, explicit: explicit,
                    changes: changes, cost: cost)
    end

    public

    # Reopens the written file, expands it, and compares per-institution
    # views. Returns the verification section of the report.
    def verify_file(path = @output_path)
      @actual = {}
      if path && File.exist?(path)
        bib = BibTeX.open(path, parse_names: false, parse_months: false)
        bib.replace
        bib.join
        bib.entries.each_value { |entry| @actual[entry.key] = entry }
      end
      verify
      @report['verification']
    end

    private

    def verify
      issues = []
      return @report = report_body(issues) if @actual.nil?

      expected_records.each do |child|
        actual = @actual[child.formal_key]
        if actual.nil?
          issues << {
            'institution' => child.institution,
            'original_key' => child.local_key,
            'formal_key' => child.formal_key,
            'field' => nil,
            'problem' => 'missing entry'
          }
          next
        end

        union = (child.view.keys + actual.fields.keys +
                 BibMerge.alias_names(actual.fields)).uniq - [:crossref]
        union.each do |name|
          wanted = child[name]
          got = actual.get(name)
          got = got.to_s unless got.nil?
          next if wanted == got

          issues << {
            'institution' => child.institution,
            'original_key' => child.local_key,
            'formal_key' => child.formal_key,
            'field' => name.to_s,
            'expected' => wanted,
            'actual' => got,
            'problem' => if wanted.nil?
                           'extra'
                         elsif got.nil?
                           'missing'
                         else
                           'different'
                         end
          }
        end
      end

      # Parents are not query targets, but a parent field that answers an
      # alias must still be the value the keeping institution had.
      @plans.each do |formal, plan|
        next if plan.parent_index.nil?

        actual = @actual[formal]
        next if actual.nil?

        parent = plan.conference.parents.fetch(plan.parent_index)
        parent.forms.each do |name, form|
          observed = actual.get(name)
          observed = observed.to_s unless observed.nil?
          next if observed == form.value

          issues << {
            'institution' => parent.institution,
            'original_key' => parent.key,
            'formal_key' => formal,
            'field' => name.to_s,
            'expected' => form.value,
            'actual' => observed,
            'problem' => 'parent field differs from its source'
          }
        end
      end

      @report = report_body(issues)
    end

    def expected_records
      @conferences.values.flat_map(&:children) +
        (@duplicates || []) + @citations
    end

    def report_body(issues)
      {
        'counts' => {
          'changed_child_fields' => all_child_plans.sum { |plan| plan.changes.length },
          'crossref_deletions' => all_child_plans.sum do |plan|
            plan.changes.count { |c| c['action'] == 'delete_crossref' }
          end
        },
        'macros' => macro_report,
        'parents' => parent_report,
        'alternatives' => alternatives_report,
        'child_changes' => all_child_plans.flat_map(&:changes),
        'parent_field_changes' => parent_field_changes,
        'components' => @component_groups.map do |group|
          component = { 'macros' => group[:macros], 'conferences' => group[:conferences] }
          component['citations'] = group[:citations].map(&:formal_key) unless group[:citations].empty?
          component
        end,
        'verification' => {
          'status' => issues.empty? ? 'ok' : 'mismatch',
          'issues' => issues
        }
      }
    end

    # Every macro/parent combination for the conferences that depend on a
    # macro, with its cost, so the choice can be audited. Independent groups
    # are evaluated one at a time, never as a product.
    def alternatives_report
      @component_groups.flat_map do |group|
        next [] if group[:macros].empty?

        group[:conferences].flat_map do |formal|
          conference = @conferences[formal]
          alternatives_for(conference, group[:macros])
        end
      end
    end

    def alternatives_for(conference, macros)
      options = []
      macro_assignments(macros).each do |assignment|
        expanded = expansions(assignment)
        next if expanded.nil?

        ([nil] + (0...conference.parents.length).to_a).each do |index|
          plan = plan_conference(conference, index, assignment, expanded)
          next if plan.nil?

          options << {
            'macros' => assignment.transform_values { |v| v.nil? ? nil : "candidate #{v}" },
            'parent' => index,
            'changed_child_fields' => plan.cost
          }
        end
      end

      [{
        'formal' => conference.formal,
        'options' => options.sort_by do |option|
          [option['changed_child_fields'],
           option['parent'].nil? ? 10_000 : option['parent']]
        end
      }]
    end

    def all_child_plans
      @all_child_plans ||= @plans.values.flat_map(&:children) + @citation_plans
    end

    def macro_report
      @manifest.fetch('macros').filter_map do |spec|
        index = @macro_choice[spec['formal']]
        next if index.nil?

        candidate = @macros.fetch(spec['formal']).candidates.fetch(index)
        {
          'formal' => spec['formal'],
          'institution' => candidate.institution,
          'original_key' => candidate.key.to_s,
          'candidate_index' => index,
          'value' => candidate.form.expression
        }
      end
    end

    def parent_report
      @manifest.fetch('conferences').filter_map do |spec|
        plan = @plans[spec['formal']]
        next if plan.nil? || plan.parent_index.nil?

        parent = plan.conference.parents.fetch(plan.parent_index)
        {
          'formal' => spec['formal'],
          'institution' => parent.institution,
          'original_key' => parent.key,
          'candidate_index' => plan.parent_index
        }
      end
    end

    def parent_field_changes
      @manifest.fetch('conferences').flat_map do |spec|
        plan = @plans[spec['formal']]
        next [] if plan.nil? || plan.parent_index.nil?

        parent = plan.conference.parents.fetch(plan.parent_index)
        plan.parent_forms.filter_map do |name, form|
          original = parent.forms.fetch(name)
          next if original.tokens == form.tokens

          {
            'formal' => spec['formal'],
            'institution' => parent.institution,
            'original_key' => parent.key,
            'field' => name.to_s,
            'from' => original.expression,
            'to' => form.value
          }
        end
      end
    end
  end
end

if $PROGRAM_NAME == __FILE__
  options = {}
  OptionParser.new do |parser|
    parser.banner = 'Usage: ruby tools/bibmerge.rb --manifest MANIFEST --out FILE [--report FILE]'
    parser.on('--manifest PATH', 'Handover manifest (JSON)') { |v| options[:manifest] = v }
    parser.on('--out PATH', 'Merged BibTeX file') { |v| options[:out] = v }
    parser.on('--report PATH', 'JSON report') { |v| options[:report] = v }
    parser.on('--strict', 'Exit 1 if no equivalent merge exists') { options[:strict] = true }
  end.parse!

  abort 'missing --manifest or --out' unless options[:manifest] && options[:out]

  manifest_path = File.expand_path(options[:manifest])
  root = File.dirname(manifest_path)
  manifest = JSON.parse(File.read(manifest_path, encoding: 'utf-8'))

  begin
    merger = BibMerge::Merger.new(manifest, root: root).run
  rescue BibMerge::UnrepresentableError => e
    warn "no equivalent merge: #{e.message}"
    e.conflicts.each { |conflict| warn JSON.generate(conflict) }
    if options[:report]
      File.write(
        options[:report],
        JSON.pretty_generate(
          'status' => 'no equivalent merge',
          'reason' => e.message,
          'conflicts' => e.conflicts
        ),
        encoding: 'utf-8'
      )
    end
    exit(options[:strict] ? 1 : 2)
  end

  merger.output_path = File.expand_path(options[:out])
  File.write(options[:out], merger.to_bibtex, encoding: 'utf-8')
  merger.verify_file
  if options[:report]
    File.write(options[:report], JSON.pretty_generate(merger.report), encoding: 'utf-8')
  end

  unless merger.report.dig('verification', 'status') == 'ok'
    warn 'verification failed'
    merger.report.dig('verification', 'issues').each { |issue| warn JSON.generate(issue) }
    exit 3
  end
end
