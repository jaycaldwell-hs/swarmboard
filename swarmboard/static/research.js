"use strict";
(() => {
  const toggles = ["dedup", "loop_prevention", "cooldowns", "dormancy"];

  function creationOptions(formData) {
    if (formData.get("research") !== "on") return {};
    const nullableNumber = name => {
      const value = formData.get("policy_" + name);
      return value == null || value === "" ? null : Number(value);
    };
    return {session_type: "research", policy: {
      profile: formData.get("policy_profile") || "production",
      ...Object.fromEntries(toggles.map(name => [name, formData.get("policy_" + name) === "on"])),
      cooldown_seconds: nullableNumber("cooldown_seconds"),
      consecutive_turn_cap: nullableNumber("consecutive_turn_cap"),
      schema_mode: formData.get("policy_schema_mode") || "strict",
    }};
  }

  function bind(root) {
    const field = name => root.querySelector(`[name="${name}"]`);
    const policy = root.querySelector("[data-research-policy]");
    const updateVisibility = () => {
      policy.hidden = !field("research").checked;
      policy.disabled = policy.hidden;
    };
    field("research").addEventListener("change", updateVisibility);
    field("policy_profile").addEventListener("change", () => {
      const profile = field("policy_profile").value;
      if (profile === "custom") return;
      const production = profile === "production";
      toggles.forEach(name => { field("policy_" + name).checked = production; });
      field("policy_cooldown_seconds").value = "";
      field("policy_consecutive_turn_cap").value = production ? "1" : "";
      field("policy_schema_mode").value = production ? "strict" : "capture";
    });
    policy.addEventListener("input", event => {
      if (event.target !== field("policy_profile")) field("policy_profile").value = "custom";
    });
    updateVisibility();
  }

  window.SwarmResearch = {creationOptions, bind};
  document.addEventListener("DOMContentLoaded", () => document.querySelectorAll("[data-research-options]").forEach(bind));
})();
