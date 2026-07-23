/* Pure OLRESULTS2 business rules shared by the browser and frontend tests. */
"use strict";

(function exposeDomainRules(root, factory) {
  const rules = Object.freeze(factory());
  if (typeof module !== "undefined" && module.exports) module.exports = rules;
  root.OLRDomainRules = rules;
}(typeof globalThis !== "undefined" ? globalThis : this, () => {
  // SEASON-001/002: calendar year for every discipline except Ski-O.
  // The Ski-O season beginning in November is named after the following
  // calendar year.
  const seasonYear = (dateStr, sportType) => {
    if (!dateStr) return "";
    const year = Number(String(dateStr).slice(0, 4));
    const month = Number(String(dateStr).slice(5, 7));
    if (!Number.isInteger(year) || !Number.isInteger(month)) return "";
    return String(
      sportType === "skiOrienteering" && month >= 11 ? year + 1 : year,
    );
  };

  return { seasonYear };
}));
