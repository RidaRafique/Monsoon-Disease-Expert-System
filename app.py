import streamlit as st
import pandas as pd


# 1. KNOWLEDGE BASE

class KnowledgeBase:
    """Loads disease rules from CSV."""

    REQUIRED_COLUMNS = ["disease", "symptoms", "first_aid"]

    OPTIONAL_COLUMNS = [
        "soft_excluded_symptoms",
        "soft_exclusion_penalty",
        "duration_symptom",
        "min_days",
        "max_days",
        "duration_mode",
    ]

    def __init__(self, filename="monsoon_diseases.csv"):
        self.filename = filename
        self.diseases = {}
        self.all_symptoms = set()

    @staticmethod
    def _parse_symptoms(value):
        if pd.isna(value) or not str(value).strip():
            return []
        return [
            item.strip().lower()
            for item in str(value).split(";")
            if item.strip()
        ]

    @staticmethod
    def _optional_number(value, field_name, row_number):
        if pd.isna(value) or not str(value).strip():
            return None
        try:
            number = float(value)
            if number < 0:
                raise ValueError
            return number
        except (TypeError, ValueError):
            raise ValueError(
                f"Invalid {field_name} value in CSV row {row_number}: {value}"
            )

    def load(self):
        try:
            df = pd.read_csv(self.filename)

            missing = [
                column for column in self.REQUIRED_COLUMNS
                if column not in df.columns
            ]
            if missing:
                st.error(
                    "CSV is missing required columns: "
                    + ", ".join(missing)
                )
                st.stop()

            # Old three-column CSV files remain supported.
            for column in self.OPTIONAL_COLUMNS:
                if column not in df.columns:
                    df[column] = ""

            for row_index, row in df.iterrows():
                row_number = row_index + 2
                disease = (
                    str(row["disease"]).strip()
                    if pd.notna(row["disease"])
                    else ""
                )

                if not disease:
                    continue

                symptoms = self._parse_symptoms(row["symptoms"])
                if not symptoms:
                    continue

                first_aid = (
                    str(row["first_aid"]).strip()
                    if pd.notna(row["first_aid"])
                    else "Consult a healthcare professional."
                )

                soft_exclusions = self._parse_symptoms(
                    row["soft_excluded_symptoms"]
                )

                duration_symptom = (
                    str(row["duration_symptom"]).strip().lower()
                    if pd.notna(row["duration_symptom"])
                    and str(row["duration_symptom"]).strip()
                    else None
                )

                min_days = self._optional_number(
                    row["min_days"], "min_days", row_number
                )
                max_days = self._optional_number(
                    row["max_days"], "max_days", row_number
                )

                if (
                    min_days is not None
                    and max_days is not None
                    and min_days > max_days
                ):
                    raise ValueError(
                        f"min_days cannot exceed max_days (CSV row {row_number})."
                    )

                if (
                    (min_days is not None or max_days is not None)
                    and not duration_symptom
                ):
                    raise ValueError(
                        f"CSV row {row_number} has duration limits but no 'duration_symptom'. Specify the symptom whose "
                        "duration those limits describe."
                    )

                duration_mode = (
                    str(row["duration_mode"]).strip().lower()
                    if pd.notna(row["duration_mode"])
                    and str(row["duration_mode"]).strip()
                    else "informational"
                )
                if duration_mode not in {"informational", "required"}:
                    raise ValueError(
                        f"Invalid duration_mode in CSV row {row_number}. Use 'informational' or 'required'."
                    )

                penalty = self._optional_number(
                    row["soft_exclusion_penalty"],
                    "soft_exclusion_penalty",
                    row_number
                )
                if penalty is not None and penalty > 100:
                    raise ValueError(
                        f"soft_exclusion_penalty must be between 0 and 100 (CSV row {row_number})."
                    )

                if disease not in self.diseases:
                    self.diseases[disease] = {
                        "rules": [],
                        "first_aid": first_aid
                    }

                self.diseases[disease]["rules"].append({
                    "required_symptoms": symptoms,
                    "soft_excluded_symptoms": soft_exclusions,
                    "soft_exclusion_penalty": penalty,
                    "duration_symptom": duration_symptom,
                    "min_days": min_days,
                    "max_days": max_days,
                    "duration_mode": duration_mode,
                })

                self.all_symptoms.update(symptoms)
                self.all_symptoms.update(soft_exclusions)
                if duration_symptom:
                    self.all_symptoms.add(duration_symptom)

        except FileNotFoundError:
            st.error(
                f"'{self.filename}' was not found. Place it in the same "
                "folder as app.py."
            )
            st.stop()
        except pd.errors.ParserError:
            st.error(
                "The CSV could not be read. Ensure first_aid values containing commas are enclosed in double quotes."
            )
            st.stop()
        except ValueError as error:
            st.error(f"Knowledge-base configuration error: {error}")
            st.stop()

        return self


# 2. FACTS / WORKING MEMORY

class Facts:
    """
    Stores patient facts.
    symptoms is a dictionary:
        symptom -> {"severity": 1..5, "days": non-negative number}
    """

    def __init__(self, symptoms):
        self.symptoms = {}

        for symptom, details in symptoms.items():
            normalized = symptom.strip().lower()

            if isinstance(details, dict):
                severity = int(details.get("severity", 3))
                days = details.get("days")
            else:
                severity = int(details)
                days = None

            self.symptoms[normalized] = {
                "severity": max(1, min(5, severity)),
                "days": None if days is None else float(days)
            }

    def has(self, symptom):
        return symptom.strip().lower() in self.symptoms

    def severity_of(self, symptom):
        details = self.symptoms.get(symptom.strip().lower())
        return details["severity"] if details else 0

    def days_of(self, symptom):
        details = self.symptoms.get(symptom.strip().lower())
        return details["days"] if details else None

    def selected_symptoms(self):
        return list(self.symptoms.keys())


# 3. SYMPTOM RULES

class SymptomRule:
    """Checks one individual symptom against the patient's facts."""

    def __init__(self, symptom):
        self.symptom = symptom.strip().lower()

    def matches(self, facts):
        if facts.has(self.symptom):
            return True
        return False

    def apply(self, facts):
        if self.matches(facts):
            return facts.severity_of(self.symptom)
        return 0


# 4. CONDITION RULES

class ConditionRule:
    """
    Evaluates one CSV row as a compound condition rule.
    Required symptoms contribute to the original weighted score. Soft exclusions are reported as conflicting evidence. They do not
    change the score unless a configured penalty is supplied. Duration limits refer only to duration_symptom, never to the longest
    duration among all selected symptoms.
    """

    def __init__(self, rule_data, symptom_rules):
        self.required_symptoms = rule_data["required_symptoms"]
        self.soft_exclusions = rule_data["soft_excluded_symptoms"]
        self.soft_penalty = rule_data["soft_exclusion_penalty"]
        self.duration_symptom = rule_data["duration_symptom"]
        self.min_days = rule_data["min_days"]
        self.max_days = rule_data["max_days"]
        self.duration_mode = rule_data["duration_mode"]
        self.symptom_rules = symptom_rules

    def evaluate(self, facts):
        matched = []
        missing = []
        triggered_soft = [
            symptom for symptom in self.soft_exclusions
            if facts.has(symptom)
        ]

        for symptom in self.required_symptoms:
            rule = self.symptom_rules.get(symptom)
            if rule and rule.matches(facts):
                matched.append(symptom)
            else:
                missing.append(symptom)

        trace = {
            "matched": matched,
            "missing": missing,
            "soft_exclusions": triggered_soft,
            "duration_symptom": self.duration_symptom,
            "duration_days": None,
            "min_days": self.min_days,
            "max_days": self.max_days,
            "duration_status": "Not configured",
            "duration_mode": self.duration_mode,
            "rejected_reason": None,
            "soft_penalty_applied": 0.0,
        }

        # A duration condition is checked only when its named symptom is selected and a duration limit is configured.
        if (
            self.duration_symptom
            and facts.has(self.duration_symptom)
            and (self.min_days is not None or self.max_days is not None)
        ):
            reported_days = facts.days_of(self.duration_symptom)
            trace["duration_days"] = reported_days

            if reported_days is None:
                trace["duration_status"] = (
                    f"Duration for {self.duration_symptom} was not provided"
                )
                if self.duration_mode == "required":
                    trace["rejected_reason"] = (
                        "Required duration information is missing"
                    )
                    return 0.0, trace, False
            else:
                too_short = (
                    self.min_days is not None
                    and reported_days < self.min_days
                )
                too_long = (
                    self.max_days is not None
                    and reported_days > self.max_days
                )

                if too_short or too_long:
                    bounds = []
                    if self.min_days is not None:
                        bounds.append(f"at least {self.min_days:g} day(s)")
                    if self.max_days is not None:
                        bounds.append(f"at most {self.max_days:g} day(s)")
                    trace["duration_status"] = (
                        f"Informational note: "
                        f"{self.duration_symptom.title()} duration "
                        f"({reported_days:g} day(s)) falls outside the "
                        "typical course range ("
                        + " and ".join(bounds)
                        + "), but score is not penalized."
                    )
                    if self.duration_mode == "required":
                        trace["rejected_reason"] = (
                            "Required duration condition was not satisfied"
                        )
                        return 0.0, trace, False
                else:
                    trace["duration_status"] = (
                        f"{self.duration_symptom.title()} duration "
                        f"({reported_days:g} day(s)) satisfies the configured "
                        "range"
                    )

        match_score = 0
        for symptom in matched:
            match_score += self.symptom_rules[symptom].apply(facts)

        total_score = len(self.required_symptoms) * 5
        if total_score == 0:
            return 0.0, trace, False

        # Preserve the original weighted matching formula.
        match_percent = (match_score / total_score) * 100

        # A soft penalty is only applied when a value is explicitly provided in the CSV. No penalty is invented by the application.
        if triggered_soft and self.soft_penalty is not None:
            penalty = self.soft_penalty
            match_percent *= (1 - penalty / 100)
            trace["soft_penalty_applied"] = penalty

        trace["score_explanation"] = (
            f"{match_score} severity point(s) / "
            f"{total_score} possible point(s)"
        )
        return match_percent, trace, True


# 5. INFERENCE ENGINE

class InferenceEngine:
    """
    Evaluates every condition rule and returns the top three diseases.
    Each disease's highest-scoring applicable rule is used, matching the original application's best-combination-per-disease behavior.
    """

    def __init__(self, knowledge_base):
        self.symptom_rules = {
            symptom: SymptomRule(symptom)
            for symptom in knowledge_base.all_symptoms
        }
        self.condition_rules = {
            disease: [
                ConditionRule(rule_data, self.symptom_rules)
                for rule_data in data["rules"]
            ]
            for disease, data in knowledge_base.diseases.items()
        }

    def diagnose(self, facts):
        results = []

        for disease, rules in self.condition_rules.items():
            best_score = 0.0
            best_trace = None
            rejected_traces = []

            for rule in rules:
                score, trace, applicable = rule.evaluate(facts)

                if not applicable:
                    rejected_traces.append(trace)
                    continue

                if score > best_score:
                    best_score = score
                    best_trace = trace

            if best_trace is not None and best_score > 0:
                results.append({
                    "disease": disease,
                    "score": best_score,
                    "evidence": best_trace["matched"],
                    "trace": best_trace,
                    "other_rejected_rules": rejected_traces,
                })

        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:3]


# 6. DECISION SUPPORT SYSTEM

class DecisionSupportSystem:
    """Emergency symptom checks and severity-based advice."""

    EMERGENCY_SYMPTOMS = {
        "difficulty breathing",
        "rapid breathing",
        "chest pain when breathing",
        "bloody diarrhea",
        "profuse watery diarrhea",
        "dehydration",
        "reduced urination",
        "jaundice",
        "difficulty opening mouth",
        "difficulty swallowing",
        "muscle spasms",
    }

    def suggest_action(self, facts, knowledge_base):
        if not facts.symptoms:
            return "Please select at least one symptom."

        available_emergency = (
            self.EMERGENCY_SYMPTOMS.intersection(
                knowledge_base.all_symptoms
            )
        )
        missing_emergency = (
            self.EMERGENCY_SYMPTOMS - knowledge_base.all_symptoms
        )

        if missing_emergency:
            st.warning(
                "Some emergency symptoms are not present in the CSV symptom selector: " + ", ".join(sorted(missing_emergency))
            )

        if set(facts.symptoms).intersection(available_emergency):
            return (
                "A potentially serious warning symptom has been selected. Seek urgent medical assessment or emergency care."
            )

        severities = [
            details["severity"] for details in facts.symptoms.values()
        ]
        average_severity = sum(severities) / len(severities)
        maximum_severity = max(severities)

        if maximum_severity >= 5 or average_severity >= 4:
            return "Seek prompt medical attention."
        elif average_severity >= 2.5:
            return "Consult a doctor for medical evaluation."
        else:
            return (
                "Consult a healthcare professional if symptoms persist or worsen."
            )


# 7. STREAMLIT APPLICATION

def get_match_strength(score: float) -> str:
    if score >= 60:
        return "Strong match"
    elif score >= 30:
        return "Moderate match"
    else:
        return "Low match"


def format_duration_info(trace: dict) -> str:
    status = trace.get("duration_status", "Not configured")
    if status == "Not configured":
        return ""
    
    symptom = trace.get("duration_symptom", "").title()
    days = trace.get("duration_days")
    
    if "falls outside" in status:
        bounds = []
        if trace.get("min_days") is not None:
            bounds.append(f"at least {trace['min_days']:g} days")
        if trace.get("max_days") is not None:
            bounds.append(f"up to {trace['max_days']:g} days")
        if not bounds:
            return (
                f"Your {symptom.lower()} has lasted {days:g} days. Symptom duration varies from person to person."
            )
        typical = " and ".join(bounds)
        return (
            f"Your {symptom.lower()} has lasted {days:g} days. "
            f"General information describes a typical duration of {typical}. This does not rule out the condition."
        )
    elif "satisfies" in status:
        return (
            f"Your {symptom.lower()} duration ({days:g} day(s)) "
            f"falls within the typical range."
        )
    elif "not provided" in status:
        return (
            f"Duration for {symptom.lower()} was not provided. "
            f"General information describes a typical course, but this does not rule out the condition."
        )
    return status


def render_medical_warning():
    st.error(
        "**Important Health Warning**\n\n"
        "If you experience difficulty breathing, severe chest pain, confusion, unconsciousness, or other severe symptoms, "
        "**seek emergency medical care immediately.**\n\n"
    )
    st.divider()


st.set_page_config(page_title="Monsoon Disease Expert System", layout="centered")
st.title("Monsoon Disease Expert System")
st.caption(
    "Educational symptom-matching support only. It does not provide a confirmed medical diagnosis."
)

knowledge_base = KnowledgeBase().load()
inference_engine = InferenceEngine(knowledge_base)
decision_support = DecisionSupportSystem()

if "diagnosis" not in st.session_state:
    st.session_state.diagnosis = None


# SYMPTOM INPUT

st.header("Select Symptoms, Severity & Duration")

st.markdown(
    """
    <style>
    div[data-baseweb="popover"] ul {
        max-height: 180px;
    }
    </style>
    """,
    unsafe_allow_html=True
)

all_symptoms = sorted(knowledge_base.all_symptoms)
selected = st.multiselect(
    "Select the symptoms the patient is experiencing:",
    all_symptoms
)

selected_symptoms = {}

if selected:
    st.caption(
        "Enter how severe each symptom is (1 = mild, 5 = very severe) and how many days the patient has experienced that symptom."
    )

    for symptom in selected:
        st.markdown(f"**{symptom.title()}**")
        severity = st.slider(
            f"Severity of {symptom}",
            min_value=1,
            max_value=5,
            value=3,
            key=f"severity_{symptom}"
        )
        duration = st.number_input(
            f"Duration of {symptom} (days)",
            min_value=0.0,
            value=1.0,
            step=1.0,
            key=f"duration_{symptom}"
        )
        selected_symptoms[symptom] = {
            "severity": severity,
            "days": duration
        }

if st.button("Diagnose"):
    if not selected:
        st.session_state.diagnosis = None
        st.warning("Please select at least one symptom.")
    else:
        facts = Facts(selected_symptoms)
        diagnoses = inference_engine.diagnose(facts)
        advice = decision_support.suggest_action(
            facts, knowledge_base
        )

        st.session_state.diagnosis = {
            "diagnoses": diagnoses,
            "advice": advice,
            "symptoms": facts.symptoms.copy()
        }

diagnosis = st.session_state.diagnosis

if diagnosis:
    diagnoses = diagnosis["diagnoses"]
    advice = diagnosis["advice"]
    facts = Facts(diagnosis["symptoms"])

    # Medical warning section - always visible
    render_medical_warning()

    # Results summary header
    st.header("Your Symptom Assessment")
    st.write(
        "Here are the conditions that most closely match the symptoms you entered."
    )
    st.write(f"**{len(diagnoses)} result{'s' if len(diagnoses) != 1 else ''}**")

    # Check for equal scores
    scores = [d["score"] for d in diagnoses]
    all_equal = len(set(round(s, 1) for s in scores)) == 1 and len(diagnoses) > 1

    if all_equal:
        st.info(
            "All matching conditions have equal symptom match scores. the order does not indicate one is more likely than another."
        )

    if diagnoses:
        for index, result in enumerate(diagnoses, 1):
            trace = result["trace"]
            disease = result["disease"]
            score = result["score"]
            match_strength = get_match_strength(score)
            first_aid = knowledge_base.diseases[disease]["first_aid"]

    # Simple card layout
            with st.container():
                st.subheader(
                    f"{index}. {disease} — {score:.1f}% match"
                )
                st.write(f"**{match_strength}**")

                # Symptoms you reported
                if trace["matched"]:
                    st.markdown("**Symptoms you reported**")
                    st.write(", ".join(s.title() for s in trace["matched"]))

                # Other commonly associated symptoms
                if trace["missing"]:
                    st.markdown("**Other commonly associated symptoms**")
                    st.write(", ".join(s.title() for s in trace["missing"]))

                # Expandable details
                with st.expander("View details", expanded=False):
                    # Soft exclusions
                    if trace["soft_exclusions"]:
                        st.markdown("**Symptoms that may suggest other conditions**")
                        st.write(", ".join(trace["soft_exclusions"]))
                        if trace["soft_penalty_applied"] > 0:
                            st.caption(
                                f"Configured penalty applied: {trace['soft_penalty_applied']:g}%"
                            )

                    # Duration information
                    duration_info = format_duration_info(trace)
                    if duration_info:
                        st.markdown("**About symptom duration**")
                        st.write(duration_info)

                    # What you can do
                    st.markdown("**What you can do**")
                    # Parse first_aid into bullet points if it contains semicolons
                    if ";" in first_aid:
                        for item in first_aid.split(";"):
                            item = item.strip()
                            if item:
                                st.write(f"- {item}")
                    else:
                        st.write(f"- {first_aid}")


                st.divider()
    else:
        st.write(
            "No applicable disease rules matched the reported symptoms. This does not mean that the patient is disease-free."
        )


    st.write(f"**Decision support advice:** {advice}")
