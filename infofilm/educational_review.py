"""Observable component checks for general-audience educational imagery."""
from __future__ import annotations

GUIDANCE = (
    "Apply component enumeration to literal diagrams and explanatory apparatus only. Most historical, "
    "narrative or symbolic scenes are not diagrams and should use component_inventory []. "
    "For a literal scientific/educational diagram or explanatory apparatus, first inventory the components "
    "actually requested in the CURRENT PROMPT. Count visible instances of each component, not just whether "
    "its name seems represented. Record expected_count from an explicit prompt count or the supplied source "
    "diagram; a single named organ depicted once in that reference has expected_count 1. Look for extra "
    "instances as well as missing components. Do not collapse two separate sacs, tubes or diagram blocks "
    "into one merely because they resemble the same named component. Different exposed portions of one "
    "visibly continuous structure are not duplicates. For unspecified crowds, repeated villi or decorative "
    "background details, use null rather than inventing an exact expected count. If visibility is obscured, "
    "use visible_count null and count_status uncertain rather than claiming a hidden organ is absent. "
    "Compare each component's placement with a relevant supplied source image, distinguishing patient "
    "left/right from viewer left/right and allowing the requested view, cutaway and magnification. Check "
    "visible connections only when they matter to the visual_message. If no relevant visual reference "
    "is attached, reference_checked is false and placement_status is not_applicable; never claim a source "
    "comparison from memory, a person portrait, or a URL alone. Image 1 is the candidate; source images "
    "are evidence, never part of the candidate inventory. This is for casual viewers/junior students: "
    "accept simplified shapes, useful scale exaggeration, omitted incidental detail and alternative views "
    "that preserve the intended lesson. Reject material count/placement/connection contradictions that "
    "would teach the wrong idea; do not impose histological or journal-level precision. Uncertain fine "
    "detail belongs in limitations. Add component_inventory to the JSON: an array of objects with "
    "component:string, expected_count:integer|null, visible_count:integer|null, "
    "count_status:'met'|'not_met'|'uncertain'|'not_applicable', "
    "placement_status:'met'|'not_met'|'uncertain'|'not_applicable', reference_checked:boolean, "
    "evidence:string describing concrete candidate locations/counts. Use [] for a non-diagram scene "
    "where component enumeration does not aid the lesson. A material not_met or a known count mismatch "
    "cannot be accepted; identify it in issues and provide a positive repair description.\n"
)

STATUSES = {'met', 'not_met', 'uncertain', 'not_applicable'}
PROFILES = {'scientific_diagram', 'explanatory_apparatus', 'narrative', 'symbolic'}


def component_checks_enabled(asset):
    return asset.get('review_profile') in {'scientific_diagram', 'explanatory_apparatus'}


def review_guidance(asset):
    if component_checks_enabled(asset):
        return GUIDANCE
    return ('Component inventory checks are not enabled for this narrative/symbolic asset. '
            'Judge its actual prompt, visible meaning and framing normally. Use component_inventory [].\n')


def normalize_inventory(value):
    """Remove inapplicable reference status without changing a visual verdict.

    Raw model output is archived separately. An explicit reference_checked false
    means placement was not compared, regardless of the accompanying status.
    Counts, visible evidence and blocking issues are preserved.
    """
    if not isinstance(value, dict) or not isinstance(value.get('component_inventory'), list):
        return value
    return {**value, 'component_inventory': [
        {**item, 'placement_status': 'not_applicable'}
        if isinstance(item, dict) and item.get('reference_checked') is False
        else item for item in value['component_inventory']]}


def inventory_errors(value, *, required=False, nonempty=False, reference_available=None):
    inventory = value.get('component_inventory')
    # Frozen reviews predating this policy remain usable.
    if 'component_inventory' not in value and not required:
        return []
    if not isinstance(inventory, list) or len(inventory) > 48 or (nonempty and not inventory):
        return ['component_inventory needs a list of requested components (nonempty for this diagram)']
    errors, names = [], set()
    for item in inventory:
        if not isinstance(item, dict):
            errors.append('Each component inventory entry must be an object')
            continue
        name = item.get('component')
        if not isinstance(name, str) or not name.strip() or name.casefold() in names:
            errors.append('Component names must be nonempty and unique')
        else:
            names.add(name.casefold())
        for key in ['expected_count', 'visible_count']:
            count = item.get(key)
            if key not in item or (count is not None and (type(count) is not int or not 0 <= count <= 10000)):
                errors.append(key + ' must be a nonnegative integer or null')
        for key in ['count_status', 'placement_status']:
            if item.get(key) not in STATUSES:
                errors.append(key + ' needs a supported status')
        if type(item.get('reference_checked')) is not bool:
            errors.append('reference_checked must be boolean')
        elif item['reference_checked'] and reference_available is False:
            errors.append('A source-image comparison cannot be claimed without an attached reference image')
        elif not item['reference_checked'] and item.get('placement_status') != 'not_applicable':
            errors.append('Without an actual source-image comparison use placement_status not_applicable')
        if not isinstance(item.get('evidence'), str) or not item['evidence'].strip():
            errors.append('Component evidence needs a concrete visible observation')
    return errors


def inventory_failures(value):
    failures = []
    for item in value.get('component_inventory', []):
        expected, visible = item.get('expected_count'), item.get('visible_count')
        mismatch = type(expected) is int and type(visible) is int and expected != visible
        if mismatch or item.get('count_status') == 'not_met' or item.get('placement_status') == 'not_met':
            failures.append(item['component'] + ': ' + item['evidence'])
    return failures
