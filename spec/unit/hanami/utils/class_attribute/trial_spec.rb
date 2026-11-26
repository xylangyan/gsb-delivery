# frozen_string_literal: true

require "hanami/utils/class_attribute"

RSpec.describe Hanami::Utils::ClassAttribute::Trial do
  before do
    class TrialStation
      include Hanami::Utils::ClassAttribute

      class_attribute :routes, :preview

      self.routes = self.preview = [{match: :all, target: :main}]
    end

    class NorthTrialStation < TrialStation
    end

    class SouthTrialStation < TrialStation
    end
  end

  after do
    %i[TrialStation NorthTrialStation SouthTrialStation].each do |const|
      Object.send :remove_const, const
    end
  end

  it "isolates writes from production" do
    Hanami::Utils::ClassAttribute.trial do
      NorthTrialStation.routes << {match: :oversize, target: :oversize}
      expect(NorthTrialStation.routes.length).to eq(2)
    end

    expect(NorthTrialStation.routes).to eq([{match: :all, target: :main}])
  end

  it "keeps inner shared rules shared within the window" do
    Hanami::Utils::ClassAttribute.trial do
      NorthTrialStation.routes.first[:target] = :trial

      expect(NorthTrialStation.preview.first[:target]).to eq(:trial)
      expect(SouthTrialStation.routes.first[:target]).to eq(:trial)
      expect(SouthTrialStation.preview.first[:target]).to eq(:trial)
    end

    expect(NorthTrialStation.routes.first[:target]).to eq(:main)
    expect(SouthTrialStation.preview.first[:target]).to eq(:main)
  end

  it "lets temporary subclasses inherit the window values" do
    Hanami::Utils::ClassAttribute.trial do
      NorthTrialStation.routes << {match: :oversize, target: :oversize}
      temp = Class.new(NorthTrialStation)

      expect(temp.routes.length).to eq(2)
      expect(temp.preview.length).to eq(1)

      temp.routes.first[:target] = :trial
      expect(NorthTrialStation.routes.first[:target]).to eq(:trial)
      expect(SouthTrialStation.preview.first[:target]).to eq(:trial)
    end
  end

  it "is scoped to the current fiber" do
    box = []
    first = Fiber.new do
      Hanami::Utils::ClassAttribute.trial do
        NorthTrialStation.routes << {match: :oversize, target: :oversize}
        NorthTrialStation.routes.first[:target] = :trial
        Fiber.yield
        box << NorthTrialStation.routes.length
        box << NorthTrialStation.routes.first[:target]
      end
    end
    first.resume

    second = Fiber.new do
      Hanami::Utils::ClassAttribute.trial do
        box << NorthTrialStation.routes.length
        box << NorthTrialStation.routes.first[:target]
        NorthTrialStation.routes.first[:target] = :other
        NorthTrialStation.routes = []
      end
    end
    second.resume

    expect(box).to eq([1, :main])

    first.resume
    expect(box).to eq([1, :main, 2, :trial])
    expect(NorthTrialStation.routes).to eq([{match: :all, target: :main}])
  end

  it "rolls back a nested window that raises" do
    Hanami::Utils::ClassAttribute.trial do
      NorthTrialStation.routes << {match: :oversize, target: :oversize}
      temp = Class.new(NorthTrialStation)
      saved_routes = NorthTrialStation.routes
      saved_rule = NorthTrialStation.routes.first
      SouthTrialStation.preview.first[:target] = :trial

      begin
        Hanami::Utils::ClassAttribute.trial do
          NorthTrialStation.routes = []
          expect(NorthTrialStation.routes).to eq([])
          SouthTrialStation.preview.first[:target] = :quarantine
          raise "abort"
        end
      rescue RuntimeError
      end

      expect(NorthTrialStation.routes).to be(saved_routes)
      expect(NorthTrialStation.routes.length).to eq(2)
      expect(saved_rule[:target]).to eq(:trial)
      expect(temp.routes.first[:target]).to eq(:trial)
    end
  end

  it "merges a nested window that ends normally" do
    Hanami::Utils::ClassAttribute.trial do
      saved_rule = NorthTrialStation.routes.first
      SouthTrialStation.preview.first[:target] = :trial

      Hanami::Utils::ClassAttribute.trial do
        SouthTrialStation.preview.first[:target] = :staging
      end

      expect(saved_rule[:target]).to eq(:staging)
      expect(NorthTrialStation.preview.first[:target]).to eq(:staging)
    end

    expect(NorthTrialStation.routes.first[:target]).to eq(:main)
  end

  it "runs the full receiving preview scenario" do
    outer = nil

    first_window = Fiber.new do
      Hanami::Utils::ClassAttribute.trial do
        NorthTrialStation.routes << {match: :oversize, target: :oversize}

        temp = Class.new(NorthTrialStation)
        expect(temp.routes.length).to eq(2)
        expect(temp.preview.length).to eq(1)

        saved_routes = NorthTrialStation.routes
        saved_rule = NorthTrialStation.routes.first

        SouthTrialStation.preview.first[:target] = :trial
        expect(NorthTrialStation.routes.first[:target]).to eq(:trial)
        expect(NorthTrialStation.preview.first[:target]).to eq(:trial)
        expect(SouthTrialStation.routes.first[:target]).to eq(:trial)
        expect(temp.routes.first[:target]).to eq(:trial)

        outer = {routes: saved_routes, rule: saved_rule, temp: temp}
        Fiber.yield

        begin
          Hanami::Utils::ClassAttribute.trial do
            NorthTrialStation.routes = []
            SouthTrialStation.preview.first[:target] = :quarantine
            raise "abort"
          end
        rescue RuntimeError
        end

        expect(NorthTrialStation.routes).to be(saved_routes)
        expect(saved_rule[:target]).to eq(:trial)
        expect(temp.routes.first[:target]).to eq(:trial)

        Hanami::Utils::ClassAttribute.trial do
          SouthTrialStation.preview.first[:target] = :staging
        end
        expect(saved_rule[:target]).to eq(:staging)
      end
    end
    first_window.resume

    expect(NorthTrialStation.routes).to eq([{match: :all, target: :main}])
    expect(SouthTrialStation.preview).to eq([{match: :all, target: :main}])

    Fiber.new do
      Hanami::Utils::ClassAttribute.trial do
        expect(NorthTrialStation.routes).to eq([{match: :all, target: :main}])
        expect(SouthTrialStation.preview).to eq([{match: :all, target: :main}])
        NorthTrialStation.routes.first[:target] = :other
        NorthTrialStation.preview << {match: :all, target: :other}
      end
    end.resume

    first_window.resume

    expect(outer[:rule][:target]).to eq(:staging)
    expect(NorthTrialStation.routes).to eq([{match: :all, target: :main}])
    expect(SouthTrialStation.routes).to eq([{match: :all, target: :main}])
    expect(SouthTrialStation.preview).to eq([{match: :all, target: :main}])
    expect(TrialStation.routes).to eq([{match: :all, target: :main}])
  end

  it "supports configurations made of arrays, hashes and scalars" do
    Hanami::Utils::ClassAttribute.trial do
      NorthTrialStation.preview = {rules: [{target: :main}], retries: 3, label: "north"}
      NorthTrialStation.preview[:rules] << {target: :trial}
      NorthTrialStation.preview[:retries] += 1

      expect(NorthTrialStation.preview[:rules].length).to eq(2)
      expect(NorthTrialStation.preview[:retries]).to eq(4)
    end

    expect(NorthTrialStation.preview).to eq([{match: :all, target: :main}])
  end
end
