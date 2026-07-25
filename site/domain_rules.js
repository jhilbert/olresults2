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

  // SQLite person IDs are signed 64-bit integers. Historical fallback IDs
  // intentionally use that full range and can therefore exceed JavaScript's
  // Number.MAX_SAFE_INTEGER. Keep them as decimal text in URLs, state and SQL
  // parameters; converting one to Number silently points at another person.
  const personIdString = (value) => {
    if (value == null || value === "") return null;
    if (typeof value === "number" && !Number.isSafeInteger(value)) return null;
    const text = String(value);
    return /^-?\d+$/.test(text) ? text : null;
  };

  const REGIONAL_CATEGORY_PREFIX = Object.freeze({
    WIEN: "Wien",
    NOE: "NÖ",
    BGLD: "Burgenland",
    STMK: "Steiermark",
    OOE: "OÖ",
    SBG: "Salzburg",
    TIR: "Tirol",
    KTN: "Kärnten",
    VBG: "Vorarlberg",
  });

  const categoryGenderGroup = (category) => {
    const text = String(category || "").trim();
    if (/\b(?:damen|frauen|women)\b/i.test(text)
        && !/\b(?:herren|männer|men)\b/i.test(text)) return 0;
    if (/\b(?:herren|männer|men)\b/i.test(text)
        && !/\b(?:damen|frauen|women)\b/i.test(text)) return 1;
    const compact = text
      .replace(/^(?:Wien|NÖ|NOE|Burgenland|Steiermark|OÖ|OOE|Salzburg|Tirol|Kärnten|Vorarlberg)\s+/i, "");
    if (/^D(?=\s|-|\d)/i.test(compact) && !/^DH(?=\s|-|\d)/i.test(compact)) return 0;
    if (/^H(?=\s|-|\d)/i.test(compact)) return 1;
    return 2;
  };

  // Fachliche Ergebnisreihenfolge: zuerst Damen, dann Herren, danach
  // gemischte/offene/Rahmenklassen; innerhalb eines Blocks jung nach alt.
  const compareResultCategories = (left, right) => {
    const a = String(left || ""), b = String(right || "");
    const groupDiff = categoryGenderGroup(a) - categoryGenderGroup(b);
    if (groupDiff) return groupDiff;
    const age = (value) => {
      const match = value.match(/\d{1,3}/);
      return match ? Number(match[0]) : 999;
    };
    const ageDiff = age(a) - age(b);
    return ageDiff || a.localeCompare(b, "de", { numeric: true, sensitivity: "base" });
  };

  const regionalCategoryLabel = (jurisdiction, category) => {
    const prefix = REGIONAL_CATEGORY_PREFIX[jurisdiction] || jurisdiction || "";
    const source = String(category || "").trim().replace(/\s+/g, " ");
    const match = source.match(/^(DH|D|H)(?=\s|-|\d|$)\s*(.*)$/i);
    if (!match) return [prefix, source].filter(Boolean).join(" ");
    const gender = match[1].toUpperCase() === "D" ? "Damen"
      : match[1].toUpperCase() === "H" ? "Herren" : "Damen und Herren";
    let ageClass = match[2].trim().replace(/\s+/g, " ");
    const exactAge = ageClass.match(/^(\d{1,3})$/);
    if (exactAge) {
      const age = Number(exactAge[1]);
      ageClass = age <= 18 ? `-${age}` : `${age}-`;
    }
    ageClass = ageClass
      .replace(/^-\s+/, "-")
      .replace(/\s+-$/, "-")
      .replace(/^(\d{1,3})\s*E$/i, "$1 Elite");
    return [prefix, gender, ageClass].filter(Boolean).join(" ");
  };

  return {
    seasonYear,
    personIdString,
    compareResultCategories,
    regionalCategoryLabel,
  };
}));
