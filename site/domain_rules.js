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

  // Only age classes with a clear upper bound between 12 and 18 count.
  // In particular, 15- is an open adult class, not the youth class 15-18.
  const youthTitleCategory = (category) => {
    const text = String(category || "").replace(/[–—−]/g, "-").trim();
    const range = text.match(/(?:^|\D)(\d{1,2})\s*(?:-|bis)\s*(\d{1,2})(?!\d)/i);
    const ceiling = text.match(/(?:\bbis\s*|(^|[^\d])-\s*)(1[2-8])(?!\d)/i);
    const compact = text.match(/(?:^|\s)(?:D|H|DH)\s*(12|14|16|18)(?![\d-])(?:\s|$)/i);
    const maxAge = range ? Number(range[2]) : ceiling ? Number(ceiling[2])
      : compact ? Number(compact[1]) : null;
    if (maxAge == null || maxAge < 12 || maxAge > 18) return null;
    const mixed = /(?:damen|frauen)\s*(?:\+|&|und)\s*(?:herren|männer)|(?:herren|männer)\s*(?:\+|&|und)\s*(?:damen|frauen)|(?:^|\s)DH(?=\s|-|\d)|\bmixed\b|\bnachwuchs\b/i.test(text);
    const female = /\b(?:damen|frauen)(?=\b|\d)|(?:^|\s)D(?=\s|-|\d)/i.test(text);
    const male = /\b(?:herren|männer)(?=\b|\d)|(?:^|\s)H(?=\s|-|\d)/i.test(text);
    return { maxAge, gender: mixed || (female && male) ? null : female ? "D" : male ? "H" : null };
  };

  const youthTitleGrant = (kind, gender) => kind === "om" ? 400
    : kind === "wien" && gender === "D" ? 110
    : kind === "wien" && gender === "H" ? 90 : null;

  // One championship_entry represents one competing person, pair or team.
  // A started competitor counts even with DNF, MP or DSQ; DNS and unknown
  // statuses cannot establish a start. Do not restrict this to the displayed
  // club/person: the minimum applies to the full championship category.
  const regionalStarterCountsSql = () => `
    SELECT starter.championship_instance_id, COUNT(*) AS starter_count
      FROM championship_entry starter
     WHERE starter.eligibility_state = 'eligible'
       AND EXISTS (
         SELECT 1 FROM championship_entry_result starter_result
         JOIN result started ON started.id = starter_result.result_id
          WHERE starter_result.championship_entry_id = starter.id
            AND started.out_of_competition = 0
            AND started.status IN ('ok', 'dnf', 'mp', 'dsq'))
     GROUP BY starter.championship_instance_id`;

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

  const regionalCategoryLabel = (jurisdiction, category, sourceCategory = "") => {
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
      // The canonical category key discards the dash. Preserve an explicit
      // lower-bound source such as "Damen 15-", including after "Wien".
      const source = String(sourceCategory || "");
      const explicitMinimum = new RegExp(`(?:\\bab\\s*${age}(?!\\d)|(?:^|[^0-9-])${age}\\s*-(?!\\s*\\d))`, "i").test(source);
      ageClass = age <= 18 && !explicitMinimum ? `-${age}` : `${age}-`;
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
    youthTitleCategory,
    youthTitleGrant,
    regionalStarterCountsSql,
  };
}));
