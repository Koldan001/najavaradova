import json
import time
from typing import Literal
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
import config
from core.api_budget import try_consume_budget
from core.rate_limiter import wait_for_slot

# Hard per-call limit. With none, an overloaded model held the whole pipeline: on 24 Sep 2026
# gemini-3.5-flash answered after ~10 minutes, twice in a row, while EPS had ~130 rows queued.
GEMINI_TIMEOUT_MS = config.GEMINI_TIMEOUT_S * 1000
# On "503 high demand" try the same model this many times, then move down the cascade, instead
# of spending the full 6+12+18+24 s backoff on a model that is overloaded for everyone.
TRANSIENT_TRIES_PER_MODEL = 2

client = genai.Client(api_key=config.API_KEY,
                      http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))


def _is_timeout(e: Exception) -> bool:
    return "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower()

class StreetSegment(BaseModel):
    location_type: Literal["STREET", "ENTIRE_SETTLEMENT", "VECTOR", "LANDMARK"] = Field(
        description="Type of location matching."
    )
    settlement: str | None = Field(
        description="Exact settlement/village in SERBIAN CYRILLIC (e.g., 'ВЕТЕРНИК', 'ЧЕНЕЈ', 'БАЈМОК', 'БЕЛО БЛАТО'). Leave unset if not specified — callers fall back to the announcement's own Opština/city, not a fixed default.",
        default=None
    )
    main_street: str | None = Field(
        description="Clean, raw main street name ONLY in SERBIAN CYRILLIC (e.g., 'ФУТОШКА', 'МАТИЈЕ ГУПЦА', 'В.Р.ПУТНИКА'). Strip house numbers, parna/neparna, landmarks, and 'od/do' directions.",
        default=None
    )
    parity: Literal["PARNA", "NEPARNA", "ALL"] = Field(
        description="Parity rule for house numbers.",
        default="ALL"
    )
    number_range: str | None = Field(
        description="Numeric range if present (e.g., '2-6', '20-33'). Do NOT include words like 'od' or 'do'.",
        default=None
    )
    explicit_numbers: str | None = Field(
        description="Comma-separated explicit house numbers if listed (e.g., '41, 43, 50', '40, 42, 44').",
        default=None
    )
    intersection_start: str | None = Field(
        description="Starting cross street or anchor in SERBIAN CYRILLIC if vector bounds ('od...').", 
        default=None
    )
    intersection_end: str | None = Field(
        description="Ending cross street or anchor in SERBIAN CYRILLIC if vector bounds ('do...').", 
        default=None
    )
    landmark: str | None = Field(
        description="Landmark or shop name mentioned in SERBIAN CYRILLIC (e.g., 'РЕСТОРАН ВУЛИН', 'КАРИН КОМЕРЦ').",
        default=None
    )
    scope: Literal["WHOLE", "PART", "SPOT"] | None = Field(
        description=(
            "ONLY for ENTIRE_SETTLEMENT, how much of the settlement the text says is affected: "
            "WHOLE ('цело насеље', 'цело село', or the place named with nothing narrowing it), "
            "PART ('део села X', 'делови насеља X' with no specific spot named), SPOT (a block, "
            "a hamlet/засеок, the centre, a building or 'код …', a weekend zone inside it: "
            "'Блок 8 у Петроварадину', 'Здравиње - засеок Поток'). Put the spot itself in "
            "'landmark'. Leave unset for any other location type."
        ),
        default=None
    )
    includes_surrounding_streets: bool = Field(
        description=(
            "True when the text says the outage reaches BEYOND the named street(s) into unnamed "
            "nearby ones — 'и суседним улицама', 'и околне улице', 'у ширем рејону око', "
            "'у околини', 'у рејону око'. False when only the named streets are affected."
        ),
        default=False
    )
    time_window: str = Field(
        description="Duration extracted from table row (e.g., '08:30 - 11:30'). See rule 8 for "
                    "what to do when the source doesn't state an exact clock time - never '00:00'."
    )

class OutageAlert(BaseModel):
    event_type: str = Field(default="POWER")
    city: str = Field(description="Overarching Opština column in SERBIAN CYRILLIC.")
    outage_date: str = Field(description="Start date in YYYY-MM-DD or DD.MM.YYYY format.")
    end_date: str | None = Field(
        description=(
            "The LAST day the works/interruption are stated to run, in DD.MM.YYYY, ONLY when the "
            "text gives a multi-day span or an explicit finish date - '15.-16.08.2026', 'од 12. "
            "до 20. септембра', 'радови ће трајати до петка 05.09.2026', 'у периоду 23.-24.'. "
            "Leave null for a same-day interruption or an open-ended one ('до завршетка радова', "
            "'до нормализације')."
        ),
        default=None,
    )
    road_closure_only: bool = Field(
        description=(
            "True when the notice is ONLY about closed/diverted traffic ('обустава саобраћаја', "
            "'измена режима саобраћаја') with NO stated interruption to water/electricity/heating. "
            "False whenever a utility supply is also cut."
        ),
        default=False,
    )
    message_template: str = Field(
        description=(
            "ONE short notification message in Serbian Ekavica, SERBIAN LATIN SCRIPT ONLY, "
            "containing the literal placeholder *** exactly once where the affected street name "
            "belongs. The placeholder is substituted per recipient with their own street, so the "
            "sentence must read naturally with a street name in that slot. Include the date and "
            "the time window if known. Keep it to one or two sentences — this is a push "
            "notification, not a press release. "
            "CRITICAL: name the utility the announcement is actually about (struja / voda / "
            "grejanje) and NEVER guess one. If the text does not say, write a neutral "
            "'Planirano isključenje u ulici ***' rather than inventing a utility - telling "
            "someone their water is going off when it is their electricity is worse than "
            "telling them nothing specific. "
            "For a toplana (district heating) notice say what people LOSE, never the utility's "
            "own word 'toplotna energija': 'tople vode' when the text is about potrošna / "
            "sanitarna topla voda (PTV), 'grejanja' when it is about heating, both only when "
            "the text cuts both. 'Грејно подручје' is the plant's service AREA, not heating "
            "being cut. Works on the network ('радови на топловодној мрежи', 'изградња "
            "топловода') that don't say anything is cut are works, not a cut: 'Radovi na "
            "toplovodnoj mreži u ulici *** dana ...'. "
            "Examples: 'Planirano isključenje struje u ulici *** dana 26.08.2026. u periodu "
            "08:00 - 12:00.' / 'Bez tople vode u ulici *** dana 26.08.2026. u periodu "
            "08:00 - 12:00.'"
        ),
        default="Planirano isključenje u ulici ***.",
    )
    affected_locations: list[StreetSegment]
    excluded_locations: list[StreetSegment] = Field(
        default_factory=list,
        description="Streets/house-number ranges explicitly EXCLUDED from an otherwise broad outage "
                    "(e.g. 'ceo naselje OSIM ulica X i Y', 'izuzev', 'sem'). Use the SAME StreetSegment "
                    "shape as affected_locations (main_street + parity/number_range/explicit_numbers) "
                    "to describe exactly what's excluded. Empty list if nothing is excluded."
    )

SYSTEM_INSTRUCTION = """
You are an expert GIS and Serbian Utility Data Extraction Parser.
Read unstructured Serbian utility descriptions and normalize them into strict structured records.

CRITICAL EXTRACTION RULES:
1. MULTI-STREET SPLITTING: If text lists multiple streets (e.g., "u ulicama: Matije Gupca, Milosa Obilica, Marka Kraljevica"), you MUST output a SEPARATE StreetSegment object for EVERY individual street.
2. 'main_street': MUST contain ONLY the official street name in SERBIAN CYRILLIC.
   - Example Input: "В.Р.Путника парна од 20 до М.Требињца" -> main_street: "В.Р.ПУТНИКА", parity: "PARNA", number_range: "20", intersection_end: "М.ТРЕБИЊЦА"
   - Example Input: "у улици Матије Гупца бројеви 41, 43 и 50" -> main_street: "МАТИЈЕ ГУПЦА", explicit_numbers: "41, 43, 50"
   - Example Input (a bulleted per-street list, no 'бројеви'/'од-до' keyword at all - the number
     range sits bare right after the street name): "– Бате Бркића 12-32;\n– Браће Дроњак 11-17;"
     -> TWO segments: main_street "БАТЕ БРКИЋА" number_range "12-32"; main_street "БРАЋЕ ДРОЊАК"
     number_range "11-17". A bare street name with NO trailing number at all in this same list
     format (e.g. "– Каће Дејановић.") means the WHOLE street - leave number_range/parity/
     explicit_numbers unset for that one, don't inherit a neighbouring line's range.
3. 'settlement': Extract sub-settlements/villages (e.g. 'Ветерник', 'Бајмок', 'Хоргош', 'Бело Блато') from text prefixes like 'Ветерник;', 'u naselju Bajmok', 'Хоргош:'.
3a. MULTI-SETTLEMENT SPLITTING: "без воде су потрошачи у следећим насељима: Шумарице, Вишњак, део
    Станова, део Корићана и део Ердеча" — output a SEPARATE ENTIRE_SETTLEMENT StreetSegment for
    EVERY named area, each with its 'settlement' set ('ШУМАРИЦЕ', 'ВИШЊАК', 'СТАНОВО', 'КОРИЋАНИ',
    'ЕРДЕЧ'). Drop the qualifier 'део' ('part of') from the name itself. NEVER emit an
    ENTIRE_SETTLEMENT with an empty 'settlement'.
4. 'message_template': Output exactly ONE message, Serbian Ekavica in SERBIAN LATIN script, with the
   literal placeholder *** standing in for the street name (e.g., 'Planirano isključenje struje u
   ulici *** dana 26.08.2026. u periodu 08:00 - 12:00.'). Do NOT write a separate message per
   street and do NOT inline any actual street name into it — one template covers every street in
   this announcement, and *** is replaced per recipient with the street they actually live on.
5. EXCLUSIONS ('осим' / 'izuzev' / 'sem'): If the text says a broad area (e.g. a whole naselje/settlement)
   is affected EXCEPT specific streets or house-number ranges, put those specific streets/ranges into
   'excluded_locations' (NOT into 'affected_locations' — do not double-list them as if they were also
   affected). This is the opposite of rule #1: the excepted streets are being carved OUT, not added.
   'number_range' only holds ONE range (e.g. "17-53") — if a street lists several disjoint ranges,
   output a SEPARATE excluded_locations StreetSegment per range, same as the multi-street splitting in
   rule #1.
   - Example Input: "Чалма: Већи део села осим, Ул. Планинска 17-53, 14-66 и бб" -> affected_locations
     contains a single ENTIRE_SETTLEMENT segment for Чалма; excluded_locations contains TWO STREET
     segments for ПЛАНИНСКА (one with number_range "17-53", one with number_range "14-66"), plus a third
     for the "бб" (no house number) properties on that same street if distinguishable.
6. SURROUNDING STREETS ('и суседним улицама' / 'и околне улице' / 'у ширем рејону око' / 'у околини' /
   'у рејону око'): the outage spills past the named street into nearby unnamed ones. Set
   'includes_surrounding_streets': true on EVERY StreetSegment the phrase covers. Still name the
   streets that ARE listed — the flag widens them, it does not replace them. Do NOT invent segments
   for the unnamed neighbours.
   - Example Input: "прекид водоснабдевања у Тоше Јовановића и суседним улицама на потезу од Коче
     Коларова до Београдске" -> ONE STREET segment: main_street "ТОШЕ ЈОВАНОВИЋА",
     intersection_start "КОЧЕ КОЛАРОВА", intersection_end "БЕОГРАДСКА", includes_surrounding_streets true.
   - Example Input: "прекид водоснабдевања у рејону око Пољопривредне школе (Николе Тесле, Иве Лоле
     Рибара, Македонска, Солунска и околне улице)" -> FOUR STREET segments (one per named street),
     each with includes_surrounding_streets true and landmark "ПОЉОПРИВРЕДНА ШКОЛА".
7. WHOLE-PLACE OUTAGES CAUSED BY A SINGLE STREET'S FAULT: when the fault is located in one street
   but the interruption covers the whole place ('санација квара ... у улици 3. октобар код броја 25,
   прекид водоснабдевања у целом насељеном месту'), the AFFECTED location is the whole place —
   output ENTIRE_SETTLEMENT with 'main_street' left unset. The street where the digging happens is
   NOT the affected area and must not become a STREET segment; it only narrows who is affected when
   the text itself limits the interruption to that street.
8. TIME WINDOWS ('time_window'): every notification needs a stated time, but NEVER default a
   missing start to '00:00' - a fault report with no clock time in it does not mean the crew
   started at midnight, and showing midnight is actively misleading. Instead:
   - No start time stated anywhere in the text -> use '07:00' (when utility crews actually start).
   - No end time at all -> use '19:00' (typical end of a working day), UNLESS the text itself
     gives an open-ended phrase worth keeping as-is instead of a clock time ('до завршетка радова',
     'до нормализације' / 'until works are completed') - keep that wording rather than inventing one.
   - Vague time-of-day words instead of a clock time - map to these canonical hours:
     'јутарњи часови' / ujutru / morning -> 07:00; 'преподне' / forenoon -> 10:00;
     'послеподне' / afternoon -> 15:00; 'вечерњи часови' / evening -> 19:00;
     'ноћни часови' / night -> 22:00.
     Example: "нормализација у вечерњим часовима" with nothing else stated -> time_window
     '07:00 - 19:00' (no other time given, so the start also falls back to the rule above).
   - If the text states one side of the range explicitly (e.g. only an end time, '... до 15
     часова') keep that stated value exactly and only fall back on the rule above for the side
     that's genuinely missing.
9. DENSE UNPUNCTUATED STREET+NUMBER BLOCKS: some sources (confirmed real case, EPS Sremska
   Mitrovica rows) chain many streets with NO commas at all - each new street is only marked by
   its own 'Ул.'/'Улица' prefix, immediately followed by its own numbers:
   "Ул. Деспота Бранковића од бр. 1-19 и 18-30 и б.б. Ул. Његошева од бр. 33-75 и 36-48 Ул.
   Славонска Ул. Алексе Шантића од бр. 7-31 и 6-24". Treat every 'Ул./Улица' occurrence as the
   start of a new street, ending where the next one begins - do NOT let one street's numbers
   bleed into a neighbour, and do NOT drop a street just because it isn't comma-separated.
   - 'од бр. A-B и C-D' (two ranges joined by 'и' on the SAME street) -> TWO separate StreetSegment
     objects for that one street, one per range (same principle as rule 5's "one number_range per
     segment, split on multiple disjoint ranges" - it applies here too, not just to exclusions).
     Example: "Деспота Бранковића од бр. 1-19 и 18-30" -> two segments, both main_street
     "ДЕСПОТА БРАНКОВИЋА", number_range "1-19" and number_range "18-30" respectively.
   - 'бр. A и бр. B' (bare individual numbers, not a range) -> explicit_numbers "A, B" on one
     segment, same as rule 2's existing explicit_numbers example.
   - 'б.б.' ('без броја' / no assigned house number) carries no usable number - drop it, don't
     try to encode it in number_range or explicit_numbers.
   - A street can appear MORE THAN ONCE in the same text - once as a bare landmark reference with
     no numbers at all ('Р. Поповић "Боравак" – ул. Његошева', naming a facility's street with no
     range), and again later with its real numbers ('Ул. Његошева од бр. 33-75 и 36-48'). These
     describe the SAME outage, not two different ones - output the numbered version only; do NOT
     also emit a separate whole-street segment for the bare mention of the same street name.
   - A street genuinely written with no numbers ANYWHERE in the text (e.g. "Ул. Славонска" in the
     example above, between two other streets that do have numbers) IS a real whole-street outage
     - leave it unset. Don't invent a number just because neighbouring streets in the same block
     have one.
"""

def parse_announcement(raw_text: str, max_retries: int = 4, model: str | None = None,
                       image: tuple[bytes, str] | None = None,
                       budget: tuple[str, int] | None = None) -> dict | None:
    """model overrides config.MODEL_NAME for this call - used by matchmaker.py to retry a fully
    unresolved outage against config.GEMINI_FALLBACK_MODEL_NAME (a stronger, pricier tier) rather
    than paying for it on every call. temperature=0 rather than 0.1: this is extraction, not
    creative writing, so a small amount of sampling randomness bought nothing but occasional noise
    (confirmed real case: "Церова" came back as "ЦРОВА", a dropped letter) - 0 doesn't guarantee
    the model never mistypes a word, but removes sampling as a needless extra source of it.

    image: (bytes, mime_type) of a notice published only as a scanned picture (JKP Vodovod Čačak
    does this - the post body is empty and the whole announcement is a JPG). The same multimodal
    call reads the picture directly into the OutageAlert schema; raw_text can carry the post
    title alongside it as extra context.

    Model cascade: when `model` is NOT explicitly passed (the default scraper path), a real
    quota-exhausted response (429/RESOURCE_EXHAUSTED) on one model advances to the next model in
    config.GEMINI_MODEL_CASCADE instead of retrying the same exhausted model - retrying wouldn't
    help, the daily bucket is empty until midnight. A model that answers 404 (retired or renamed)
    is skipped the same way. When `model` IS explicitly passed (matchmaker
    .py's unresolved-outage retry against GEMINI_FALLBACK_MODEL_NAME specifically), the cascade is
    skipped entirely - that call site wants exactly that one stronger tier, not a rotation.

    budget: (budget name, daily limit) in core/api_budget.py. One unit is taken before every
    request, retries included; when the budget is spent the call stops and returns None."""
    prompt = f"Analyze this utility announcement and extract the exact details:\n\n{raw_text}"
    contents = [prompt]
    if image:
        img_bytes, mime_type = image
        contents = [
            types.Part.from_bytes(data=img_bytes, mime_type=mime_type),
            "The announcement is the image above; read every street name, house number, date and "
            "time window from it. " + prompt,
        ]

    text = _generate(contents, OutageAlert, SYSTEM_INSTRUCTION, max_retries, model, budget)
    if not text:
        return None
    try:
        return OutageAlert.model_validate_json(text)
    except Exception as e:
        print(f"⚠️ AI Parsing failed non-retryable error: {e}")
        return None


class PlaceScope(BaseModel):
    place: str = Field(description="The place exactly as it was given in the question.")
    scope: Literal["WHOLE", "PART", "SPOT", "UNCLEAR"] = Field(
        description="WHOLE: the text says the whole place is affected. PART: only part of it, no "
                    "specific spot. SPOT: a block, hamlet, centre, building or zone inside it. "
                    "UNCLEAR: the text doesn't say.")


class PlaceScopes(BaseModel):
    answers: list[PlaceScope]


SCOPE_INSTRUCTION = (
    "You check one Serbian utility notice (water, power or heating). For each place you are "
    "given, say how much of that place the notice says is affected: WHOLE, PART, SPOT or "
    "UNCLEAR. Read only the notice; never guess. 'Блок 8 у Петроварадину' is SPOT for "
    "Петроварадин. 'Део села Лозовик' is PART for Лозовик. 'Без воде ће бити насеље Бегеч' is "
    "WHOLE for Бегеч."
)


def check_place_scope(raw_text: str, places: list[str]) -> dict[str, str] | None:
    """The second opinion before a whole-area alert goes out (core/send_check.py): one short
    call that only answers WHOLE / PART / SPOT / UNCLEAR per place. None when no model answered."""
    if not places:
        return {}
    prompt = ("Notice:\n" + raw_text + "\n\nPlaces:\n" + "\n".join(f"- {p}" for p in places))
    text = _generate([prompt], PlaceScopes, SCOPE_INSTRUCTION, 2, None)
    if not text:
        return None
    try:
        answers = PlaceScopes.model_validate_json(text).answers
    except Exception:
        return None
    return {a.place: a.scope for a in answers}


def _generate(contents, schema, instruction: str, max_retries: int, model: str | None,
              budget: tuple[str, int] | None = None) -> str | None:
    """One structured call through the model cascade (see parse_announcement). The reply's JSON
    text, or None."""
    if model:
        models_to_try = [model]
    else:
        # config.MODEL_NAME first always, then the cascade (minus MODEL_NAME itself if it's also
        # listed there, so it's not tried twice back to back).
        models_to_try = [config.MODEL_NAME] + [
            m for m in config.GEMINI_MODEL_CASCADE if m != config.MODEL_NAME
        ]

    def _rpm_limit_for(m: str) -> int:
        if m == config.MODEL_NAME:
            return config.GEMINI_PRIMARY_RPM_LIMIT
        if m == config.GEMINI_FALLBACK_MODEL_NAME:
            return config.GEMINI_FALLBACK_RPM_LIMIT
        return config.GEMINI_CASCADE_DEFAULT_RPM

    for model_index, resolved_model in enumerate(models_to_try):
        rpm_limit = _rpm_limit_for(resolved_model)
        is_last_model = model_index == len(models_to_try) - 1

        for attempt in range(max_retries):
            try:
                # Proactive pacing - real latency per call (~1.7-2s measured) is fast enough to
                # naturally exceed 15 RPM on its own, well before hitting any daily cap. Blocking
                # here to stay under the confirmed real RPM ceiling beats firing as fast as
                # possible and reacting to 429s after they've already happened.
                if budget and not try_consume_budget(*budget):
                    print(f"🪫 {resolved_model} daily budget '{budget[0]}' ({budget[1]}) spent - stopping.")
                    return None
                wait_for_slot(resolved_model, rpm_limit)
                response = client.models.generate_content(
                    model=resolved_model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=instruction,
                        response_mime_type="application/json",
                        response_schema=schema,
                        temperature=0.0
                    ),
                )
                if response and response.text:
                    return response.text
            except Exception as e:
                err_str = str(e)
                is_quota = any(code in err_str for code in ("429", "RESOURCE_EXHAUSTED"))
                is_transient = any(code in err_str for code in ("503", "UNAVAILABLE", "500", "INTERNAL"))
                is_gone = getattr(e, "code", None) == 404 or "NOT_FOUND" in err_str

                if _is_timeout(e):
                    # Past GEMINI_TIMEOUT_MS: the model is overloaded, and waiting on it again
                    # would cost another full timeout. Next model, or give up on the last one -
                    # the row isn't stored, so the next cycle tries it afresh.
                    print(f"⏱️ {resolved_model} timed out after {GEMINI_TIMEOUT_MS // 1000}s.")
                    if is_last_model:
                        return None
                    break
                if is_gone and not is_last_model:
                    # Retired or renamed. On 30 Sep 2026 gemini-2.5-flash-lite answered "404 ... no
                    # longer available to new users", and treating that as fatal dropped the
                    # announcement until the next cycle, with working models still untried below it.
                    print(f"⏭️ {resolved_model} not available ({err_str[:60]}). Trying the next model...")
                    break
                if is_quota and not is_last_model:
                    # Daily bucket for this model is empty - no point burning retries or backoff
                    # sleep on it, move straight to the next model in the cascade.
                    print(f"⏭️ {resolved_model} quota exhausted ({err_str[:60]}). Falling back to next model in cascade...")
                    break
                elif is_transient and not is_last_model and attempt + 1 >= TRANSIENT_TRIES_PER_MODEL:
                    print(f"⏭️ {resolved_model} still unavailable ({err_str[:60]}). Trying the next model...")
                    break
                elif is_quota or is_transient:
                    # Either this is the last model in the cascade and it's quota-exhausted too
                    # (nothing left to fall back to, so retry/backoff same as transient), or it's
                    # a genuine transient error (503/500) - confirmed real case: a live 503 during
                    # testing was previously treated as fatal and gave up immediately instead of
                    # backing off.
                    wait_time = (attempt + 1) * 6
                    print(f"⏳ Gemini {'quota' if is_quota else 'transient'} error on {resolved_model} ({err_str[:60]}). Backing off for {wait_time}s... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                else:
                    print(f"⚠️ AI Parsing failed non-retryable error: {e}")
                    return None

    print(f"❌ Failed to parse after trying {len(models_to_try)} model(s) due to rate limits.")
    return None