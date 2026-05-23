import { useEffect, useState } from "react";
import { getThermostats, type ThermostatConfig } from "../api";
import Alert from "./Alert";

/**
 * Surfaces thermostats that still need the airflow-floor fields filled in
 * after upgrading to #213.
 *
 * Pre-#213 the engine used a flat ``min_open_vents`` count.  When the new
 * fields ship, existing thermostats end up with ``total_vents_count = null``
 * and the engine falls back to the prior "≥1 open" default — safe but not
 * the proper fraction-based floor.  This banner is what asks the user to
 * finish configuring so the new safety actually applies.
 *
 * Renders nothing when every thermostat has either ``total_vents_count`` set
 * OR ``has_bypass_damper`` ticked (the two valid configurations).  Polled on
 * mount; the banner disappears automatically once the user updates a card.
 */
export default function AirflowConfigBanner() {
  const [unconfigured, setUnconfigured] = useState<ThermostatConfig[]>([]);

  useEffect(() => {
    let cancelled = false;
    getThermostats()
      .then((tcs) => {
        if (cancelled) return;
        setUnconfigured(tcs.filter((tc) => tc.total_vents_count === null && !tc.has_bypass_damper));
      })
      .catch(() => {
        /* a network blip on load is not worth nagging about — try again
           the next time the page mounts */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (unconfigured.length === 0) return null;

  return (
    <Alert variant="warning" testId="airflow-config-banner">
      <strong>Action required: configure the airflow-floor safety</strong>
      <div style={{ marginTop: "0.5rem" }}>
        {unconfigured.length === 1 ? (
          <>
            <strong>{unconfigured[0].name || unconfigured[0].thermostat_entity_id}</strong> is
            running on the transitional default (≥1 vent always open). Open its card below and fill
            in <em>total vent count</em> — or tick <em>I have a bypass damper</em> — so the proper
            airflow-floor safety takes effect.
          </>
        ) : (
          <>
            {unconfigured.length} thermostats are running on the transitional default (≥1 vent
            always open). Open each card below and fill in <em>total vent count</em> — or tick
            <em> I have a bypass damper</em>:
            <ul style={{ margin: "0.25rem 0 0 1rem" }}>
              {unconfigured.map((tc) => (
                <li key={tc.thermostat_entity_id}>{tc.name || tc.thermostat_entity_id}</li>
              ))}
            </ul>
          </>
        )}
      </div>
      <div style={{ marginTop: "0.5rem", fontSize: "0.85rem" }}>
        Closing too many vents at once raises duct static pressure and can trip a furnace
        high-limit, strain the blower, or freeze the evaporator coil. The floor keeps a fraction of
        the home's total registers open at all times.
      </div>
    </Alert>
  );
}
