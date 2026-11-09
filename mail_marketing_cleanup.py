"""Mail marketing cleanup backend (Python 3.11+).

No UI dependencies. Shared reference loading, API/cache, validation, population
dispatch, cleanup rules and output writing live here. Add future population
handlers to POPULATIONS; the UI reads their capabilities automatically.
"""
import os
import sys
import re
import json
import sqlite3
import time
import contextlib
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import urllib.request
import urllib.error
import urllib.parse

# False = require API for Pipedrive inputs; True = allow API or local exports.
# Archived-deals uploads and SQLite population inputs are unaffected.
ALLOW_MANUAL_PIPEDRIVE = False


def validate_pipedrive_mode(config):
    if ALLOW_MANUAL_PIPEDRIVE:
        return
    if config.get('mode') != 'Download via API':
        raise ValueError('Manual Pipedrive references are temporarily disabled. Use your API token.')
    if (is_campaign_population(config['population'])
            and config.get('population_mode') != 'Download via API'):
        raise ValueError('Manual Pipedrive population exports are temporarily disabled. Use your API token.')


PHONE_COLS = ['Person - Phone - ' + x for x in ('Work', 'Home', 'Mobile', 'Other')]
PERSON_COLS = ['Person - ID', 'Person - Mailing Address'] + PHONE_COLS
DEAL_COLS = ['Deal - ID', 'Deal - Title', 'Deal - Contact Group ID', 'Deal - Serial Number', 'Deal - BU Database ID', 'Deal - NCOP ID', 'Deal - Pooling Order ID', 'Deal - Unique Database ID']
ARCHIVE_COLS = ['ID', 'Title', 'Contact Group ID', 'Serial Number', 'BU Database ID', 'Contact person ID']
BU_COLS = ['id', 'Category', 'contact_group_id', 'Serial Number', 'Owner', 'First Name', 'Middle Name', 'Last Name', 'County', 'State', 'Input: Address', 'Input: City', 'Input: State', 'Input: Zip Code', 'md_address', 'md_city', 'md_state'] + ['phone' + str(i) for i in range(1, 6)]
CATEGORIES = ('Premium', 'Large', 'Mid', 'Small', 'Too Small', 'Nu')
# Only Bottoms Up is implemented. Future handlers use the same references.
# validate(path, category) -> row count
# execute(config, ready, result_folder) -> summary dictionary
POPULATIONS = {
    'Bottoms Up': {'category': True, 'validate': 'validate_db', 'execute': 'execute_bottoms_up'},
    'NCOP': {'category': False, 'validate': 'validate_ncop_db', 'execute': 'execute_ncop'},
    'Pooling Order': {'category': False, 'validate': 'validate_po_db', 'execute': 'execute_po'},
    'NTM or Non-Targeted Marketing': {'category': False, 'validate': 'validate_ntm_db', 'execute': 'execute_ntm'},
    'Deceased Owners (DEO)': {'category': False, 'validate': None, 'execute': None},
    'Conversion Campaign: Prio 2': {'category': False, 'validate': 'validate_campaign_db', 'execute': 'execute_campaign'},
    'Conversion Campaign: Prio 3': {'category': False, 'validate': 'validate_campaign_db', 'execute': 'execute_campaign'},
    'Conversion Campaign: Prio 4': {'category': False, 'validate': 'validate_campaign_db', 'execute': 'execute_campaign'},
    'JR AA Sales Recycled': {'category': False, 'validate': 'validate_campaign_db', 'execute': 'execute_campaign'},
    'Mid AA Sales Recycled': {'category': False, 'validate': 'validate_campaign_db', 'execute': 'execute_campaign'},
    'SR AA Sales Recycled': {'category': False, 'validate': 'validate_campaign_db', 'execute': 'execute_campaign'},
}


def population_settings(population):
    try:
        return POPULATIONS[population]
    except KeyError:
        raise ValueError('Select a recognized population.') from None


def require_population(population):
    settings = population_settings(population)
    if not settings['validate'] or not settings['execute']:
        raise ValueError(f'{population}: cleanup rules and column mapping are not implemented yet.')
    return settings


def selection_signature(config):
    settings = population_settings(config['population'])
    selected = config.get('category') if settings['category'] else None
    if config['population'] == 'NCOP':
        selected = config.get('ncop_scope')
    if config['population'] == 'NTM or Non-Targeted Marketing':
        selected = tuple(sorted(ntm_selected_locations(config.get('ntm_output_locations'))))
    if is_campaign_population(config['population']):
        import hashlib
        source = config.get('population_mode')
        identity = (config.get('population_file', '') if source == 'Manual files'
                    else hashlib.sha256(config.get('token', '').encode()).hexdigest())
        selected = (source, identity)
    return (config['population'], selected)


BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
def _load_endpoints():
    path = BASE / 'config' / 'endpoints.json'
    if not path.is_file():
        raise ValueError(
            'Missing config/endpoints.json. Copy endpoints.example.json to '
            'config/endpoints.json and fill in your company URLs/IDs.'
        )
    with open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


_ENDPOINTS = _load_endpoints()
PIPEDRIVE_DOMAIN = _ENDPOINTS['pipedrive_domain']
C3_DRIVE_FOLDER_ID = _ENDPOINTS.get('c3_drive_folder_id', '')
DEALS_URL = f'https://{PIPEDRIVE_DOMAIN}/deals/filter/366502'
PERSONS_URL = f'https://{PIPEDRIVE_DOMAIN}/persons/list/filter/891899'
ARCHIVE_URL = _ENDPOINTS['archive_url']

def safe_text(value):
    return '' if pd.isna(value) else str(value)

def format_bottoms_up(df: pd.DataFrame):
    columns_to_process = ['contact_group_id',
                          '# of Interests',
                          'Total Value - Low ($)',
                          'Total Value - High ($)',
                          'PDP Value ($)',
                          'phone1',
                          'phone2',
                          'phone3',
                          'phone4',
                          'phone5',
                          'contact_id',
                          'multiple_county_count',
                          'Owner ID']

    # Apply the transformation to each column
    for column in columns_to_process:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors='raise').round()
            df[column] = df[column].astype('Int64')

    if 'Input: Zip Code' in df.columns:
        df['Input: Zip Code'] = df['Input: Zip Code'].map(report_id)

def split_pipe_values(value) -> list[str]:
    """
    Split pipe-delimited values safely.
    Handles:
    - leading/trailing pipes
    - extra spaces
    - blank segments
    Examples:
      "|123|456|" -> ["123", "456"]
      " 123 | 456 " -> ["123", "456"]
      None -> []
    """
    if pd.isna(value):
        return []

    parts = [part.strip() for part in str(value).split('|')]
    return [part for part in parts if part != '']


def extract_int_token(token):
    """
    Extract the numeric part from a token and return as int.
    Handles:
    - "123" -> 123
    - "S-123" -> 123
    - " S-123 " -> 123
    - "abc123xyz" -> 123
    Returns None if no digits found.
    """
    if pd.isna(token):
        return None

    token = str(token).strip()
    if not token:
        return None

    match = re.search(r'(\d+)', token)
    if not match:
        return None

    return int(match.group(1))


def normalize_serial_token(token) -> str:
    """
    Clean a serial number token safely.
    Handles extra spaces and ignores blank pieces.
    Keeps the value as text.
    """
    if pd.isna(token):
        return ''

    token = str(token).strip()
    return token

def format_deal_title(row: pd.Series):
    first_name = str(row['First Name']).title() if not pd.isna(row['First Name']) else ''
    last_name = str(row['Last Name']).title() if not pd.isna(row['Last Name']) else ''
    owner = str(row['Owner']).title() if not pd.isna(row['Owner']) else ''
    target_county = str(row['County']).title() if not pd.isna(row['County']) else ''
    target_state = str(row['State']).title() if not pd.isna(row['State']) else ''
    
    if pd.notna(row['Last Name']):
        return f"{first_name} {last_name} {target_county} County, {target_state}"
    else:
        return f"{owner} {target_county} County, {target_state}"
    
def apply_mask(df: pd.DataFrame, mask, output_value) -> None:
    df.loc[mask, 'reason_for_removal'] = df.loc[mask, 'reason_for_removal'].apply(
        lambda lst: lst + [output_value] if isinstance(lst, list) else [output_value])
    
def format_mailing_address(row: pd.Series):
    """
    Returns a tuple: (normalized_md_address, normalized_input_address)
    followed by your df applying fallback logic later.
    """
    # ---- md address ----
    if 'md_address' in row and not pd.isna(row['md_address']):
        md_parts = [
            safe_text(row.get('md_address')),
            safe_text(row.get('md_city')),
            safe_text(row.get('md_state'))
        ]
        md_combined = ', '.join([p.strip() for p in md_parts if p.strip()])
        normalized_md = normalize_address(md_combined)
    else:
        normalized_md = ''

    # ---- input address ----
    input_parts = [
        safe_text(row.get('Input: Address')),
        safe_text(row.get('Input: City')),
        safe_text(row.get('Input: State'))
    ]
    input_combined = ', '.join([p.strip() for p in input_parts if p.strip()])
    normalized_input = normalize_address(input_combined)

    return normalized_md, normalized_input

def remove_trailing_zip(addr: str) -> str:
    """
    Remove a US ZIP code only if it appears at the very end of the address.
    Handles:
    - 12345
    - 12345-6789
    Leaves the rest of the address untouched.
    """
    if pd.isna(addr):
        return ''
    
    addr = str(addr).strip()

    # Remove trailing ZIP or ZIP+4 only at end of string
    addr = re.sub(r'(?:,\s*|\s+)?\d{5}(?:-\d{4})?\s*$', '', addr)

    return addr.strip()

def normalize_address(addr: str) -> str:
    """
    Normalize address:
    - uppercase
    - replace synonyms (ROAD -> RD, STREET -> ST, COUNTY -> CTY)
    - remove special characters
    - keep only A–Z and 0–9
    """
    if pd.isna(addr):
        return ''
    
    addr = str(addr).upper()

    # --- Step 1: Synonym dictionary ---
    replacements = {
        r'\bROAD\b': 'RD',
        r'\bSTREET\b': 'ST',
        r'\bAVENUE\b': 'AVE',
        r'\bBOULEVARD\b': 'BLVD',
        r'\bDRIVE\b': 'DR',
        r'\bLANE\b': 'LN',
        r'\bCOURT\b': 'CT',
        r'\bCOUNTY\b': 'CTY',
        r'\bCNTY\b': 'CTY',
        r'\bHIGHWAY\b': 'HWY',
        r'\bAPARTMENT\b': 'APT',
        r'\bSUITE\b': 'STE',
        r'\bTRAIL\b': 'TRL',
        r'\bCIRCLE\b': 'CIR',
        r'\bCR\b': 'CIR',
        r'\bPARKWAY\b': 'PKWY',
        r'\bNORTH\b': 'N',
        r'\bSOUTH\b': 'S',
        r'\bEAST\b': 'E',
        r'\bWEST\b': 'W',
        r'\bNORTHEAST\b': 'NE',
        r'\bSOUTHEAST\b': 'SE',
        r'\bSOUTHWEST\b': 'SW',
        r'\bNORTHWEST\b': 'NW',
        r'\bMEADOWS\b': 'MDWS',
        r'\bBUILDING\b': 'BLDG',
        r'\bEXPRESSWAY\b': 'EXPY',
        r'\bMOUNTAIN\b': 'MT',
        r'\bMOUNT\b': 'MT',
        r'\bPLACE\b': 'PL',
        r'\bSQUARE\b': 'SQ',
        r'\bBASEMENT\b': 'BSMT',
        r'\bDEPARTMENT\b': 'DEPT',
        r'\bFLOOR\b': 'FL',
        r'\bROOM\b': 'RM',
        r'\bLOWER\b': 'LOWR',
        r'\bUPPER\b': 'UPPR',
        r'\bOFFICE\b': 'OFC',
        r'\bCENTER\b': 'CTR',
        r'\bEXTENSION\b': 'EXT',
        r'\bFREEWAY\b': 'FWY',
        r'\bPOINT\b': 'PT',
        r'\bROUTE\b': 'RTE',
        r'\bSKYWAY\b': 'SKWY',
        r'\bBRANCH\b': 'BR',
        r'\bCREEK\b': 'CRK',
        r'\bESTATE\b': 'EST',
    }

    # --- Step 2: Apply replacements ---
    for pattern, replacement in replacements.items():
        addr = re.sub(pattern, replacement, addr)

    # --- Step 3: Remove all non-alphanumeric ---
    addr = re.sub(r'[^A-Z0-9]', '', addr)

    return addr

def combine_input_address(row: pd.Series) -> str:
    parts = [
        safe_text(row.get('Input: Address')),
        safe_text(row.get('Input: City')),
        safe_text(row.get('Input: State'))
    ]
    combined = ', '.join([p.strip() for p in parts if p.strip()])
    combined = remove_trailing_zip(combined)
    return normalize_address(combined)

def combine_md_address(row: pd.Series) -> str:
    parts = [
        safe_text(row.get('md_address')),
        safe_text(row.get('md_city')),
        safe_text(row.get('md_state'))
    ]
    combined = ', '.join([p.strip() for p in parts if p.strip()])
    combined = remove_trailing_zip(combined)
    return normalize_address(combined)

def format_pipedrive_df(pipedrive_df: pd.DataFrame):
    # Remove ', USA' and optional zip code at the end
    pipedrive_df['Person - Mailing Address'] = pipedrive_df['Person - Mailing Address'].str.replace(r', USA(, \d{5})?$', '', regex=True)

    # Remove zip/postal code at the end (5 digits or ZIP+4 or alphanumeric postal codes)
    pipedrive_df['Person - Mailing Address'] = pipedrive_df['Person - Mailing Address'].str.replace(r'(,?\s*\d{5}(-\d{4})?)$', '', regex=True)
    
    # Strip leading/trailing whitespace
    pipedrive_df['Person - Mailing Address'] = pipedrive_df['Person - Mailing Address'].str.strip()

def get_cid_set(df: pd.DataFrame) -> set:
    unique_contact_group_ids = set()

    for val in df['Deal - Contact Group ID'].dropna():
        for token in split_pipe_values(val):
            cid = extract_int_token(token)   # handles S-123 -> 123
            if cid is not None:
                unique_contact_group_ids.add(cid)

    return unique_contact_group_ids


def get_serial_set(df: pd.DataFrame) -> set:
    unique_serial_numbers = set()

    for val in df['Deal - Serial Number'].dropna():
        for token in split_pipe_values(val):
            cleaned = normalize_serial_token(token)
            if cleaned:
                unique_serial_numbers.add(cleaned)

    return unique_serial_numbers


def get_bu_id_set(df: pd.DataFrame) -> set:
    unique_bu_database_ids = set()

    for val in df['Deal - BU Database ID'].dropna():
        for token in split_pipe_values(val):
            bu_id = extract_int_token(token)
            if bu_id is not None:
                unique_bu_database_ids.add(bu_id)

    return unique_bu_database_ids

def get_phone_set(df):
    result = set()
    for column in PHONE_COLS:
        for value in df[column].dropna():
            for part in str(value).split(','):
                digits = re.sub(r'\D', '', re.sub(r'\.0$', '', part.strip()))
                if digits:
                    result.add(int(digits))
    return result

pd.set_option('display.max_colwidth', None)

def normalize_text(s: str) -> str:
    if pd.isna(s):
        return ''
    s = str(s).upper()
    s = re.sub(r'COUNTY', '', s)
    s = re.sub(r'[^A-Z]', '', s)
    return s

def debug_df_state(df: pd.DataFrame, label: str):
    print(f"\n===== Activity: {label} =====")
    print(f"Rows: {len(df):,}")
    print(f"Columns: {len(df.columns):,}")

    if 'contact_group_id' in df.columns:
        null_cgid = df['contact_group_id'].isna().sum()
        unique_cgid = df['contact_group_id'].nunique(dropna=True)
        dup_cgid = df['contact_group_id'].duplicated(keep=False).sum()
        print(f"contact_group_id nulls: {null_cgid:,}")
        print(f"contact_group_id unique (non-null): {unique_cgid:,}")
        print(f"Rows with duplicated contact_group_id: {dup_cgid:,}")

    if 'reason_for_removal' in df.columns:
        non_null_reason = df['reason_for_removal'].notna().sum()
        print(f"Rows with reason_for_removal populated: {non_null_reason:,}")


def debug_mask(mask, label: str):
    true_count = int(mask.sum())
    false_count = int((~mask).sum())
    print(f"{label}: matched={true_count:,} | not_matched={false_count:,}")

def normalize_deal_title_for_match(value):
    if pd.isna(value):
        return ''

    return re.sub(r'[^A-Z0-9]+', '', str(value).upper())


def build_deal_title_masks(
    generated_titles, current_titles, archived_titles
):
    import ahocorasick

    normalized = generated_titles.map(
        normalize_deal_title_for_match
    )
    patterns = set(normalized) - {''}

    if not patterns:
        empty = pd.Series(
            False, index=generated_titles.index, dtype=bool
        )
        return empty.copy(), empty.copy()

    search = ahocorasick.Automaton()

    for title in patterns:
        search.add_word(title, title)

    search.make_automaton()

    masks = []

    for label, titles in (
        ('Pipedrive', current_titles),
        ('Archived Pipedrive', archived_titles),
    ):
        matched = set()
        references = titles.dropna().map(
            normalize_deal_title_for_match
        ).unique()

        print(f'Checking {len(references):,} {label} titles...')

        for number, title in enumerate(references, 1):
            if title:
                for _, generated_title in search.iter(title):
                    matched.add(generated_title)

            if number % 25000 == 0:
                print(
                    f'{label}: checked {number:,} / '
                    f'{len(references):,} titles...'
                )

        masks.append(normalized.isin(matched))

    return tuple(masks)

# A handful of reference values (e.g. a shared office phone number) can match
# hundreds of deals at once. Listing every one of those IDs in a single Excel
# cell is not useful to a reviewer and is what made Match Details slow to
# build for categories with a lot of excluded rows. Cap the per-key listing
# and note how many were left out instead.
MAX_EVIDENCE_IDS_PER_KEY = 25

# Shared evidence output for every population.
# Match Details columns are generated by wide_match_details below.
MATCH_SUMMARY_COLUMNS = [
    'matching_status', 'existing_deal_id', 'match_deal_title', 'match_address',
    'match_phone', 'match_serial_number', 'match_source_id',
    'match_contact_group_id', 'excluded_via_contact_group',
]


def match_text(value):
    if pd.isna(value):
        return ''
    return str(value).strip()


def match_phone_keys(value):
    # Follow get_phone_set exactly for Pipedrive reference values.
    keys = set()
    for part in match_text(value).split(','):
        digits = re.sub(r'\D', '', re.sub(r'\.0$', '', part.strip()))
        if digits:
            keys.add(int(digits))
    return keys


def report_id(value):
    """Format numeric IDs without .0; preserve text IDs and leading zeros."""
    text = match_text(value)
    if text.upper() in ('NAN', '<NA>', 'NONE', 'NULL'):
        return ''
    if re.fullmatch(r'[+-]?\d+\.0+', text):
        return text.split('.')[0]
    return text


def report_phone_cell(value):
    # Preserve phone punctuation; remove numeric .0 from each list item.
    return ''.join(
        part if part in ('|', ',', ';') else report_id(part)
        for part in re.split(r'([|,;])', match_text(value))
    )


def with_reference_ids(frame, canonical, aliases, label):
    """Accept common ID headers, then require an actual ID on every row."""
    lookup = {str(c).strip().casefold(): c for c in frame.columns}
    column = next((lookup[name.casefold()] for name in (canonical, *aliases)
                   if name.casefold() in lookup), None)
    if column is None:
        raise ValueError(f'{label}: missing {canonical}. Include the actual record ID in the export.')
    frame = frame.copy()
    frame[canonical] = frame[column].map(report_id)
    if frame[canonical].eq('').any():
        raise ValueError(f'{label}: {canonical} contains blank IDs. Select a complete export.')
    return frame


class C3MatchSets(tuple):
    """Keep the existing two-set interface and retain IDs for evidence."""
    def __new__(cls, addresses, phones, id_maps):
        result = super().__new__(cls, (addresses, phones))
        result.id_maps = id_maps
        return result


WIDE_MATCH_TYPES = (
    ('PHONE', 'phone'), ('ADDRESS', 'address'), ('DEAL_TITLE', 'deal_title'),
    ('SERIAL_NUMBER', 'serial_number'), ('SOURCE_ID', 'source_id'),
    ('CONTACT_GROUP_ID', 'contact_group_id'),
)


def wide_match_details(complete_result, subset):
    id_column = complete_result.attrs['match_id_column']
    campaign = is_campaign_population(complete_result.attrs.get('match_population', ''))
    match_types = (tuple(pair for pair in WIDE_MATCH_TYPES if pair[0] not in ('DEAL_TITLE', 'SOURCE_ID'))
                   + CAMPAIGN_EXTRA_MATCH_TYPES) if campaign else WIDE_MATCH_TYPES
    group_column = complete_result.attrs.get(
        'match_group_column', 'contact_group_id' if id_column == 'id' else ''
    )
    excluded = subset.loc[subset['clean_tag'].eq('N')]
    # Pandas propagates .attrs via a deep copy on every row/Series it derives from this
    # frame; complete_result.attrs['match_details'] holds every match event for the whole
    # run, so leaving it attached here made excluded.iterrows() deep-copy that entire list
    # on every single row. Dropping it (safe: nothing below reads excluded.attrs) removes
    # that incidental O(rows x events) cost without touching any matching/cleaning logic.
    excluded.attrs = {}
    # One physical row must have one usable population ID for an unambiguous link.
    ids = excluded[id_column].map(report_id)
    if ids.eq('').any() or ids.duplicated().any():
        raise ValueError(f'Match Details requires unique nonblank {id_column} values.')
    selected = set(ids)
    _t0 = time.perf_counter()
    events = [e for e in complete_result.attrs['match_details']
              if report_id(e['cleanup_record_id']) in selected]
    print(f'  [wide_match_details] filtered {len(complete_result.attrs["match_details"]):,} '
          f'raw events to {len(events):,} in {time.perf_counter()-_t0:.1f}s')
    # record -> match type/source -> normalized key -> actual matching IDs
    matches = {}
    labels = {}
    group_members = {}
    _t0 = time.perf_counter()
    for event in events:
        source = event['reference_source']
        if source not in ('PIPEDRIVE', 'ARCHIVE', 'C3'):
            continue
        rid = report_id(event['cleanup_record_id'])
        member = report_id(event['evidence_record_id'])
        if member != rid:
            group_members.setdefault(rid, set()).add(member)
        kind = event['match_type']
        system = 'c3' if source == 'C3' else 'pipedrive'
        key = event['matched_key']
        target_id = report_id(event.get('reference_id'))
        if not key or not target_id:
            raise ValueError(f'Missing matched key or reference ID for {id_column}={rid}.')
        identity = (rid, kind, system)
        matches.setdefault(identity, {}).setdefault(key, set()).add(target_id)
        # Display readable source addresses/title candidates, not duplicate reference values.
        display = (key if kind in ('PHONE', 'SOURCE_ID', 'CONTACT_GROUP_ID')
                   else event['source_value'] if kind in ('ADDRESS', 'DEAL_TITLE')
                   else key)
        labels.setdefault((*identity, key), display)
    print(f'  [wide_match_details] built matches/labels dicts '
          f'({len(matches):,} identities) in {time.perf_counter()-_t0:.1f}s')
    rows = []
    max_pairs = {}
    _t0 = time.perf_counter()
    for _, row in excluded.iterrows():
        rid = report_id(row[id_column])
        output = {id_column: rid}
        if not group_column and not campaign:
            output['existing_deal_id'] = report_id(row.get('deal_id'))
        elif group_column:
            # Only populated when evidence came from a different group member.
            output['group_matched_member_ids'] = ' | '.join(sorted(group_members.get(rid, ())))
        for kind, name in match_types:
            for system in ('pipedrive', 'c3'):
                if system == 'c3' and kind not in ('PHONE', 'ADDRESS', 'EMAIL'):
                    continue
                if kind == 'CONTACT_GROUP_ID' and group_column != 'contact_group_id' and not campaign:
                    continue
                identity = (rid, kind, system)
                by_targets = {}
                for key, target_ids in matches.get(identity, {}).items():
                    targets = tuple(sorted(target_ids))
                    by_targets.setdefault(targets, set()).add(labels[(*identity, key)])
                # Keys may share cells only when their COMPLETE target-ID sets agree.
                for number, (targets, values) in enumerate(sorted(by_targets.items()), 1):
                    prefix = f'{name}_{system}'
                    output[f'{prefix}_keys_{number}'] = ' | '.join(sorted(values))
                    suffix = 'c3_ids' if system == 'c3' else 'deal_ids'
                    id_column_name = f'{name}_c3_ids_{number}' if system == 'c3' else f'{prefix}_deal_ids_{number}'
                    output[id_column_name] = ' | '.join(targets)
                max_pairs[(kind, name, system)] = max(
                    max_pairs.get((kind, name, system), 1), len(by_targets)
                )
        rows.append(output)
    print(f'  [wide_match_details] built {len(rows):,} output row dicts in {time.perf_counter()-_t0:.1f}s')
    _t0 = time.perf_counter()
    columns = [id_column] if campaign else [id_column, 'group_matched_member_ids' if group_column else 'existing_deal_id']
    for kind, name in match_types:
        if kind == 'CONTACT_GROUP_ID' and group_column != 'contact_group_id' and not campaign:
            continue
        for system in ('pipedrive', 'c3'):
            if system == 'c3' and kind not in ('PHONE', 'ADDRESS', 'EMAIL'):
                continue
            for number in range(1, max_pairs.get((kind, name, system), 1)+1):
                prefix = f'{name}_{system}'
                suffix = 'c3_ids' if system == 'c3' else 'deal_ids'
                id_column_name = f'{name}_c3_ids_{number}' if system == 'c3' else f'{prefix}_deal_ids_{number}'
                columns.extend([f'{prefix}_keys_{number}', id_column_name])
    if len(columns) > 16384:
        raise ValueError('Too many distinct match groups for Excel columns; evidence was not truncated.')
    print(f'  [wide_match_details] computed {len(columns):,} final columns in {time.perf_counter()-_t0:.1f}s')
    _t0 = time.perf_counter()
    details = pd.DataFrame(rows, columns=columns).fillna('')
    print(f'  [wide_match_details] built final DataFrame in {time.perf_counter()-_t0:.1f}s')
    if len(details) != len(excluded):
        raise ValueError('Match Details row count does not match the excluded Cleanup rows.')
    return details


class MatchEvidence:
    def __init__(self, population, original, references, c3):
        self.population = population
        self.id_column = {'Bottoms Up': 'id', 'NCOP': 'ncop_id', 'Pooling Order': 'po_id', 'NTM or Non-Targeted Marketing': 'id',}[population]
        self.original = original.copy()
        self.original_columns = list(original.columns)
        self.references = {
            name: with_reference_ids(frame, 'Deal - ID', ('Deal ID', 'ID'), name)
            for name, frame in references.items()
        }
        self.c3 = c3
        self.c3_phone_keys = {n for value in c3[1] for n in ncop_phone_tokens(value)}
        self.rows = []
        self.seen = set()
        self.indexes = {}
        self.capped_matches = 0
        self.title_hits = {}

    def add(self, index, kind, field, value, source, ref_field, ref_value,
            key='', value_format='RAW', reference_id=''):
        row = self.original.loc[index]
        key = report_id(key) if kind in ('PHONE', 'SOURCE_ID', 'CONTACT_GROUP_ID') else match_text(key)
        entry = {
            'population': self.population,
            'cleanup_record_id': '',
            'evidence_record_id': report_id(row[self.id_column]),
            'contact_group_id': report_id(row.get('contact_group_id')),
            'group_key': (report_id(row.get('contact_id'))
                          if self.population == 'NTM or Non-Targeted Marketing' else ''),
            'match_type': kind, 'source_field': field,
            'source_value': match_text(value), 'matched_key': key,
            'reference_source': source, 'reference_field': ref_field,
            'reference_value': match_text(ref_value),
            'reference_value_format': value_format,
            'reference_id': report_id(reference_id),
        }
        signature = tuple(entry.values())
        if signature not in self.seen:
            self.seen.add(signature)
            self.rows.append(entry)


    def equal(self, index, kind, field, value, keys, source, ref_fields, mode):
        identity = (source, tuple(ref_fields), mode)
        if identity not in self.indexes:
            lookup = {}
            reference = self.references[source]
            for ref_field in ref_fields:
                for raw, deal_id in reference[[ref_field, 'Deal - ID']].itertuples(index=False, name=None):
                    if pd.isna(raw):
                        continue
                    if mode == 'phone':
                        tokens = match_phone_keys(raw)
                    elif mode == 'integer':
                        tokens = {n for p in split_pipe_values(raw)
                                  if (n := extract_int_token(p)) is not None}
                    elif mode == 'ntm_id':
                        tokens = ntm_id_tokens(raw)
                    elif mode == 'serial':
                        tokens = set(split_pipe_values(raw))
                    elif mode == 'po_id':
                        tokens = po_id_tokens(raw)
                    else:
                        normalizer = normalize_po_address if mode == 'po_address' else normalize_address
                        tokens = {normalizer(raw)} - {''}
                    for token in tokens:
                        lookup.setdefault(token, set()).add(report_id(deal_id))
            self.indexes[identity] = lookup
        for key in keys:
            matched_ids = sorted(self.indexes[identity].get(key, ()))
            for deal_id in matched_ids[:MAX_EVIDENCE_IDS_PER_KEY]:
                # Reference raw values are no longer repeated in the user report.
                self.add(index, kind, field, value, source, '', '', key,
                         reference_id=deal_id)
            overflow = len(matched_ids) - MAX_EVIDENCE_IDS_PER_KEY
            if overflow > 0:
                self.capped_matches += 1
                self.add(index, kind, field, value, source, '', '', key,
                         reference_id=f'and {overflow:,} more')

    def c3_equal(self, index, kind, field, value, key):
        addresses, phones = self.c3
        if kind == 'ADDRESS':
            found = key in addresses
            lookup_key = match_text(key)
        elif self.population == 'Bottoms Up':
            found = str(key) in phones
            lookup_key = str(key)
        else:
            found = key in self.c3_phone_keys
            lookup_key = str(key)
        if found:
            id_maps = getattr(self.c3, 'id_maps', None)
            map_kind = 'PHONE_NUMERIC' if kind == 'PHONE' and self.population != 'Bottoms Up' else kind
            ids = id_maps.get(map_kind, {}).get(lookup_key, set()) if id_maps else set()
            if not ids:
                raise ValueError('C3 match has no c3_id. Check references again with ID-bearing C3 files.')
            for c3_id in sorted(ids):
                self.add(index, kind, field, value, 'C3', '', '', key,
                         reference_id=c3_id)

    def titles(self, candidates, masks):
        import ahocorasick
        patterns = set(candidates.map(normalize_deal_title_for_match)) - {''}
        if not patterns:
            return
        search = ahocorasick.Automaton()
        for pattern in patterns:
            search.add_word(pattern, pattern)
        search.make_automaton()
        for source, mask in masks:
            hits = {}
            reference = self.references[source]
            for raw, deal_id in reference[['Deal - Title', 'Deal - ID']].itertuples(index=False, name=None):
                normalized = normalize_deal_title_for_match(raw)
                if normalized:
                    for _, pattern in search.iter(normalized):
                        hits.setdefault(pattern, set()).add(report_id(deal_id))
            for index, candidate in candidates.items():
                selected = mask.loc[index]
                matched = bool(selected.any()) if isinstance(selected, pd.Series) else bool(selected)
                if matched:
                    key = normalize_deal_title_for_match(candidate)
                    matched_ids = sorted(hits.get(key, ()))
                    for deal_id in matched_ids[:MAX_EVIDENCE_IDS_PER_KEY]:
                        self.add(index, 'DEAL_TITLE', 'generated_deal_title', candidate,
                                 source, '', '', key, reference_id=deal_id)
                    overflow = len(matched_ids) - MAX_EVIDENCE_IDS_PER_KEY
                    if overflow > 0:
                        self.capped_matches += 1
                        self.add(index, 'DEAL_TITLE', 'generated_deal_title', candidate,
                                 source, '', '', key, reference_id=f'and {overflow:,} more')


    def existing(self, index):
        value = self.original.at[index, 'deal_id']
        self.add(index, 'EXISTING_DEAL_ID', 'deal_id', value,
                 'SOURCE_DATABASE', 'deal_id', value)

    def address(self, index, columns, key, source):
        row = self.original.loc[index]
        value = ', '.join(match_text(row[c]) for c in columns if match_text(row[c]))
        field = ' + '.join(columns)
        if source == 'C3':
            self.c3_equal(index, 'ADDRESS', field, value, key)
        else:
            mode = 'po_address' if self.population == 'Pooling Order' else 'address'
            self.equal(index, 'ADDRESS', field, value, {key}, source,
                       ['Person - Mailing Address'], mode)

    def contact_values(self, index, row, phone_columns, source, phone_hit,
                       serial_hit=False, id_hit=False):
        if phone_hit:
            for field in phone_columns:
                value = row[field]
                keys = ncop_phone_tokens(value)
                if source == 'C3':
                    for key in keys:
                        self.c3_equal(index, 'PHONE', field, self.original.at[index, field], key)
                else:
                    self.equal(index, 'PHONE', field, self.original.at[index, field],
                               keys, source, PHONE_COLS, 'phone')
        if serial_hit:
            self.equal(index, 'SERIAL_NUMBER', 'serial_number', row['serial_number'],
                       set(split_pipe_values(row['serial_number'])), source,
                       ['Deal - Serial Number'], 'serial')
        if id_hit:
            is_po = self.population == 'Pooling Order'
            field = 'po_id' if is_po else 'ncop_id'
            key = ncop_text(row[field]) if is_po else int(row[field])
            self.equal(index, 'SOURCE_ID', field, row[field], {key}, source,
                       ['Deal - Pooling Order ID' if is_po else 'Deal - NCOP ID'],
                       'po_id' if is_po else 'integer')

    def has_value(self, kind, row):
        is_bu = self.population == 'Bottoms Up'
        is_po = self.population == 'Pooling Order'
        if kind == 'DEAL_TITLE':
            if is_bu:
                return bool(normalize_deal_title_for_match(format_deal_title(row)))
            return bool(po_titles(row) if is_po else ncop_titles(row))
        if kind == 'ADDRESS':
            columns = (('Input: Address', 'Input: City', 'Input: State',
                        'md_address', 'md_city', 'md_state') if is_bu else
                       tuple(c for fields in (PO_ADDRESS_SETS if is_po else NCOP_ADDRESS_SETS).values()
                             for c in fields))
            return any(match_text(row.get(c)) for c in columns)
        if kind == 'PHONE':
            columns = [f'phone{i}' for i in range(1, 6)] if is_bu else (PO_PHONE_COLS if is_po else NCOP_PHONE_COLS)
            return any(ncop_phone_tokens(row.get(c)) for c in columns)
        field = {'SERIAL_NUMBER': 'Serial Number' if is_bu else 'serial_number',
                 'SOURCE_ID': self.id_column, 'CONTACT_GROUP_ID': 'contact_group_id'}[kind]
        return bool(match_text(row.get(field)))

    def finish(self, result):
        # Map evidence from removed BU group members to the retained output row.
        result = result.copy()
        ids = result[self.id_column].map(report_id)
        retained = set(ids)
        groups = {}
        if self.population == 'Bottoms Up':
            for index, row in result.iterrows():
                if pd.notna(row['contact_group_id']):
                    groups[float(row['contact_group_id'])] = report_id(row[self.id_column])
        details = []
        for entry in self.rows:
            target = entry['evidence_record_id']
            group = entry['contact_group_id']
            if group and self.population == 'Bottoms Up':
                target = groups.get(float(group), target)
            if target in retained:
                details.append(dict(entry, cleanup_record_id=target))
        by_id = {}
        for entry in details:
            by_id.setdefault(entry['cleanup_record_id'], []).append(entry)
        for index, row in result.iterrows():
            rid = report_id(row[self.id_column])
            evidence = by_id.get(rid, [])
            own = [e for e in evidence if e['evidence_record_id'] == rid]
            inherited = self.population == 'Bottoms Up' and row['clean_tag'] == 'N' and not own and bool(evidence)
            if inherited:
                cause = evidence[0]
                details.append({
                    'population': self.population, 'cleanup_record_id': rid,
                    'evidence_record_id': cause['evidence_record_id'],
                    'contact_group_id': cause['contact_group_id'],
                    'match_type': 'CONTACT_GROUP_EXCLUSION',
                    'source_field': 'contact_group_id',
                    'source_value': cause['contact_group_id'], 'matched_key': '',
                    'reference_source': 'SOURCE_DATABASE',
                    'reference_field': 'id',
                    'reference_value': cause['evidence_record_id'],
                    'reference_value_format': 'RAW',
                })
            if row['clean_tag'] == 'N' and not evidence:
                raise ValueError(f'Missing exclusion evidence for {self.population} {rid}.')
            assigned = self.population != 'Bottoms Up' and bool(ncop_text(row.get('deal_id')))
            result.at[index, 'matching_status'] = 'SKIPPED_EXISTING_DEAL_ID' if assigned else 'CHECKED'
            result.at[index, 'existing_deal_id'] = report_id(row.get('deal_id')) if self.population != 'Bottoms Up' else 'N/A'
            for column, kind in (
                ('match_deal_title', 'DEAL_TITLE'), ('match_address', 'ADDRESS'),
                ('match_phone', 'PHONE'), ('match_serial_number', 'SERIAL_NUMBER'),
                ('match_source_id', 'SOURCE_ID'), ('match_contact_group_id', 'CONTACT_GROUP_ID'),
            ):
                matches = sorted({e['reference_source'] for e in own if e['match_type'] == kind})
                value = ' | '.join(matches) if matches else (
                    'NOT_CHECKED' if assigned else
                    'NO_MATCH' if self.has_value(kind, row) else 'NO_VALUE'
                )
                if kind == 'CONTACT_GROUP_ID' and self.population != 'Bottoms Up':
                    value = 'N/A'
                result.at[index, column] = value
            result.at[index, 'excluded_via_contact_group'] = ('Y' if inherited else 'N') if self.population == 'Bottoms Up' else 'N/A'
        # Retain original data columns; remove generated helper columns and aliases.
        original = [c for c in self.original_columns
                    if c not in MATCH_SUMMARY_COLUMNS + ['clean_tag', 'reason_for_removal', 'clean', 'summary_findings']]
        result = result[original + ['clean_tag', 'reason_for_removal'] + MATCH_SUMMARY_COLUMNS]
        # Format only exported identifiers and phone fields; never round IDs.
        numeric_columns = [self.id_column, 'contact_group_id', 'deal_id', 'existing_deal_id']
        for column in numeric_columns:
            if column in result.columns:
                result[column] = result[column].map(report_id)
        phone_columns = ([f'phone{i}' for i in range(1, 6)]
                         if self.population == 'Bottoms Up' else
                         NCOP_PHONE_COLS if self.population == 'NCOP' else PO_PHONE_COLS)
        for column in phone_columns:
            if column in result.columns:
                result[column] = result[column].map(report_phone_cell)
        only_n = set(result.loc[result['clean_tag'].eq('N'), self.id_column].map(report_id))
        result.attrs['match_details'] = [e for e in details if e['cleanup_record_id'] in only_n]
        result.attrs['match_id_column'] = self.id_column
        return result



def write_cleanup_tabs(writer, complete_result, subset):
    # subset inherits complete_result.attrs['match_details'] (a per-run evidence list that
    # can hold hundreds of thousands of entries) via pandas' attrs propagation. Pandas
    # deep-copies .attrs on every column-wise access (__finalize__), and itertuples() below
    # touches every column, so leaving this attached would re-copy that huge list dozens of
    # times and exhaust memory. Nothing past this point reads subset.attrs.
    subset.attrs = {}
    phase_start = time.perf_counter()
    details = wide_match_details(complete_result, subset)
    details.attrs = {}
    print(f'Built Match Details: {len(details):,} rows x {len(details.columns):,} columns '
          f'in {time.perf_counter()-phase_start:.1f}s')
    if len(details) > 1048575:
        raise ValueError('Match Details exceeds the Excel row limit; evidence was not truncated.')
    for frame in (subset, details):
        if len(frame.columns) > 16384:
            raise ValueError('Excel column limit exceeded; evidence was not truncated.')
    date_format = writer.book.add_format({'num_format': 'yyyy-mm-dd hh:mm:ss'})
    for name, data in (('Cleanup', subset), ('Match Details', details)):
        phase_start = time.perf_counter()
        sheet = writer.book.add_worksheet(name)
        writer.sheets[name] = sheet
        sheet.write_row(0, 0, list(data.columns))
        for number, values in enumerate(data.itertuples(index=False, name=None), 1):
            cells = []
            for value in values:
                if pd.isna(value):
                    value = None
                elif isinstance(value, np.generic):
                    value = value.item()
                if isinstance(value, str) and len(value) > 32767:
                    raise ValueError(f'Excel cell limit exceeded in {name}, row {number+1}.')
                cells.append(value)
            error = sheet.write_row(number, 0, cells, date_format)
            if error:
                raise ValueError(f'Excel write failed in {name}, row {number+1}: {error}')
        print(f'Wrote {name} sheet: {len(data):,} rows in {time.perf_counter()-phase_start:.1f}s')
    phase_start = time.perf_counter()
    for name, frame in (('Cleanup', subset), ('Match Details', details)):
        sheet = writer.sheets[name]
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, len(frame), len(frame.columns)-1)
        if name == 'Match Details':
            sheet.set_column(0, 0, 18)
            sheet.set_column(1, len(frame.columns)-1, 30)
    print(f'Applied sheet formatting in {time.perf_counter()-phase_start:.1f}s')



def cleanup(df, pipedrive_df, archived_pipedrive_df, c3_sets):
    audit = MatchEvidence('Bottoms Up', df, {
        'PIPEDRIVE': pipedrive_df.copy(), 'ARCHIVE': archived_pipedrive_df.copy(),
    }, c3_sets)
    for reference in audit.references.values():
        format_pipedrive_df(reference)
    debug_df_state(df, "After reading selected database category")
    print(f"Pipedrive rows: {len(pipedrive_df):,}")
    print(f"Archived Pipedrive rows: {len(archived_pipedrive_df):,}")

    # Get all filter sets
    print("calling get_phone_set")
    phone_numbers_set = get_phone_set(pipedrive_df)

    print("calling get_serial_set")
    serial_set = get_serial_set(pipedrive_df)

    print("calling get_cid_set")
    contact_group_id_set = get_cid_set(pipedrive_df)

    print("calling get_bu_id_set")
    bu_id_set = get_bu_id_set(pipedrive_df)

    print("calling archived get_phone_set")
    archived_phone_numbers_set = get_phone_set(archived_pipedrive_df)

    print("calling archived get_serial_set")
    archived_serial_set = get_serial_set(archived_pipedrive_df)

    print("calling archived get_cid_set")
    archived_contact_group_id_set = get_cid_set(archived_pipedrive_df)

    print("calling archived get_bu_id_set")
    archived_bu_id_set = get_bu_id_set(archived_pipedrive_df)

    print("calling format_pipedrive_df")
    format_pipedrive_df(pipedrive_df)

    pipedrive_df['normalized_pipedrive_address'] = (
        pipedrive_df['Person - Mailing Address']
        .fillna('')
        .apply(normalize_address)
    )

    print("calling format_bottoms_up")
    format_bottoms_up(df)
    debug_df_state(df, "After format_bottoms_up")
    df['reason_for_removal'] = pd.NA
    phone_columns = ['phone1', 'phone2', 'phone3', 'phone4', 'phone5',
                    #  'phone6', 'phone7', 'phone8', 'phone9', 'phone10',
                    #  'phone11',
                     ]

    # Phone number filter
    if 'phone1' in df.columns:
        phone_number_mask = df[phone_columns].isin(phone_numbers_set).any(axis=1)
        debug_mask(phone_number_mask, "Phone Number in Pipedrive")
        apply_mask(df, phone_number_mask, 'Phone Number is in Pipedrive')
        apply_mask(df, ~phone_number_mask, 'No matching Phone Number in Pipedrive - FOR MANUAL REVIEW')

        archived_phone_number_mask = df[phone_columns].isin(archived_phone_numbers_set).any(axis=1)
        debug_mask(archived_phone_number_mask, "Phone Number in Archived Pipedrive")
        apply_mask(df, archived_phone_number_mask, 'Phone Number is in Pipedrive (Archived)')
        apply_mask(df, ~archived_phone_number_mask, 'No matching Phone Number in Pipedrive (Archived) - FOR MANUAL REVIEW')

        debug_df_state(df, "After phone number filter")
    # Mailing address filter
    # Extract both addresses
    df[['md_norm', 'input_norm']] = df.apply(
        lambda r: pd.Series(format_mailing_address(r)),
        axis=1
    )

    # Priority matching:
    # 1. Match using md_norm first
    # 2. If md_norm does NOT match, try input_norm
    pipedrive_addresses = pipedrive_df['normalized_pipedrive_address'].dropna()
    pipedrive_addresses = pipedrive_addresses[pipedrive_addresses != '']  # remove empty strings
    archived_pipedrive_addresses = archived_pipedrive_df['normalized_pipedrive_address'].dropna()
    archived_pipedrive_addresses = archived_pipedrive_addresses[archived_pipedrive_addresses != '']

    md_match = df['md_norm'].isin(pipedrive_addresses)
    input_match = df['input_norm'].isin(pipedrive_addresses)

    # Final match = md_match OR input_match
    mailing_address_mask = md_match | input_match

    archived_md_match = df['md_norm'].isin(archived_pipedrive_addresses)
    archived_input_match = df['input_norm'].isin(archived_pipedrive_addresses)
    archived_mailing_address_mask = archived_md_match | archived_input_match

    debug_mask(md_match, "MD address matched Pipedrive")
    debug_mask(input_match, "Input address matched Pipedrive")
    debug_mask(mailing_address_mask, "Final mailing address matched Pipedrive")

    # Apply reasons
    apply_mask(df, mailing_address_mask, 'Found matching Mailing Address from Pipedrive')
    apply_mask(df, ~mailing_address_mask, 'No matching Mailing Address in Pipedrive - FOR MANUAL REVIEW')

    debug_mask(archived_mailing_address_mask, "Final mailing address matched Archived Pipedrive")
    apply_mask(df, archived_mailing_address_mask, 'Found matching Mailing Address from Pipedrive (Archived)')
    apply_mask(df, ~archived_mailing_address_mask, 'No matching Mailing Address in Pipedrive (Archived) - FOR MANUAL REVIEW')


    debug_df_state(df, "After mailing address filter")

    # Deal title filter: normalized substring matching
    df['deal_title'] = df.apply(format_deal_title, axis=1)

    deal_title_mask, archived_deal_title_mask = (
        build_deal_title_masks(
            df['deal_title'],
            pipedrive_df['Deal - Title'],
            archived_pipedrive_df['Deal - Title'],
        )
    )

    debug_mask(
        deal_title_mask,
        "Deal Title matched Pipedrive",
    )
    apply_mask(
        df,
        deal_title_mask,
        'Found matching Deal Title from Pipedrive',
    )
    apply_mask(
        df,
        ~deal_title_mask,
        'No matching Deal Title in Pipedrive - FOR MANUAL REVIEW',
    )

    debug_mask(
        archived_deal_title_mask,
        "Deal Title matched Archived Pipedrive",
    )
    apply_mask(
        df,
        archived_deal_title_mask,
        'Found matching Deal Title from Pipedrive (Archived)',
    )
    apply_mask(
        df,
        ~archived_deal_title_mask,
        'No matching Deal Title in Pipedrive (Archived) - FOR MANUAL REVIEW',
    )

    debug_df_state(df, "After deal title filter")
    
    # Serial Number filter
    serial_filter_mask = df['Serial Number'].astype(str).str.strip().isin(serial_set)
    debug_mask(serial_filter_mask, "Serial Number matched Pipedrive")
    apply_mask(df, serial_filter_mask, 'Serial Number is in Pipedrive')
    apply_mask(df, ~serial_filter_mask, 'No matching Serial Number in Pipedrive - FOR MANUAL REVIEW')

    archived_serial_filter_mask = df['Serial Number'].astype(str).str.strip().isin(archived_serial_set)
    debug_mask(archived_serial_filter_mask, "Serial Number matched Archived Pipedrive")
    apply_mask(df, archived_serial_filter_mask, 'Serial Number is in Pipedrive (Archived)')
    apply_mask(df, ~archived_serial_filter_mask, 'No matching Serial Number in Pipedrive (Archived) - FOR MANUAL REVIEW')

    debug_df_state(df, "After serial number filter")

    # contact_group_id filter
    contact_group_id_mask = df['contact_group_id'].isin(contact_group_id_set)
    debug_mask(contact_group_id_mask, "Contact Group ID matched Pipedrive")
    apply_mask(df, contact_group_id_mask, 'Contact Group ID is in Pipedrive')
    apply_mask(df, ~contact_group_id_mask, 'No matching Contact Group ID in Pipedrive - FOR MANUAL REVIEW')

    archived_contact_group_id_mask = df['contact_group_id'].isin(archived_contact_group_id_set)
    debug_mask(archived_contact_group_id_mask, "Contact Group ID matched Archived Pipedrive")
    apply_mask(df, archived_contact_group_id_mask, 'Contact Group ID is in Pipedrive (Archived)')
    apply_mask(df, ~archived_contact_group_id_mask, 'No matching Contact Group ID in Pipedrive (Archived) - FOR MANUAL REVIEW')

    debug_df_state(df, "After contact_group_id filter")

    # BUDB ID filter
    bu_id_mask = df['id'].isin(bu_id_set)
    debug_mask(bu_id_mask, "Bottoms Up ID matched Pipedrive")
    apply_mask(df, bu_id_mask, 'Bottoms Up ID is in Pipedrive')
    apply_mask(df, ~bu_id_mask, 'No matching Bottoms Up ID in Pipedrive - FOR MANUAL REVIEW')

    archived_bu_id_mask = df['id'].isin(archived_bu_id_set)
    debug_mask(archived_bu_id_mask, "Bottoms Up ID matched Archived Pipedrive")
    apply_mask(df, archived_bu_id_mask, 'Bottoms Up ID is in Pipedrive (Archived)')
    apply_mask(df, ~archived_bu_id_mask, 'No matching Bottoms Up ID in Pipedrive (Archived) - FOR MANUAL REVIEW')

    debug_df_state(df, "After BUDB ID filter")

    # Load C3 addresses and phones
    print("calling load_c3_from_local")
    c3_address_set, c3_phone_set = c3_sets

    # Combine input and md addresses
    print("calling combine_input_address")
    df['combined_input_address'] = df.apply(combine_input_address, axis=1)
    print("calling combine_md_address")
    df['combined_md_address'] = df.apply(combine_md_address, axis=1)

    # Address mask
    c3_address_mask = df['combined_input_address'].isin(c3_address_set) | df['combined_md_address'].isin(c3_address_set)
    debug_mask(c3_address_mask, "Address matched C3")

    apply_mask(df, c3_address_mask, 'Found address in C3 table')
    apply_mask(df, ~c3_address_mask, 'No matching address in C3 table - FOR MANUAL REVIEW')

    debug_df_state(df, "After C3 address filter")

    # Phone mask
    phone_columns = ['phone1', 'phone2', 'phone3', 'phone4', 'phone5']
    c3_phone_mask = df[phone_columns].astype(str).apply(lambda x: x.str.replace(r'\D', '', regex=True))
    c3_phone_mask = c3_phone_mask.isin(c3_phone_set).any(axis=1)
    debug_mask(c3_phone_mask, "Phone matched C3")

    apply_mask(df, c3_phone_mask, 'Phone Number is in C3 table')
    apply_mask(df, ~c3_phone_mask, 'No matching phone in C3 table - FOR MANUAL REVIEW')
    debug_df_state(df, "After C3 phone filter")

    # Capture actual positive checks before group handling and deduplication.
    for reference_source, phone_mask, address_masks, title_mask, serial_mask, group_mask, id_mask in (
        ('PIPEDRIVE', phone_number_mask,
         (('md_norm', ('md_address', 'md_city', 'md_state'), md_match),
          ('input_norm', ('Input: Address', 'Input: City', 'Input: State'), input_match)),
         deal_title_mask, serial_filter_mask, contact_group_id_mask, bu_id_mask),
        ('ARCHIVE', archived_phone_number_mask,
         (('md_norm', ('md_address', 'md_city', 'md_state'), archived_md_match),
          ('input_norm', ('Input: Address', 'Input: City', 'Input: State'), archived_input_match)),
         archived_deal_title_mask, archived_serial_filter_mask,
         archived_contact_group_id_mask, archived_bu_id_mask),
    ):
        for index in df.index[phone_mask]:
            for field in phone_columns:
                value = df.at[index, field]
                if pd.notna(value):
                    audit.equal(index, 'PHONE', field, audit.original.at[index, field],
                                {int(value)}, reference_source, PHONE_COLS, 'phone')
        for key_column, columns, mask in address_masks:
            for index in df.index[mask]:
                audit.address(index, columns, df.at[index, key_column], reference_source)
        for mask, kind, field, ref_field, mode in (
            (serial_mask, 'SERIAL_NUMBER', 'Serial Number', 'Deal - Serial Number', 'serial'),
            (group_mask, 'CONTACT_GROUP_ID', 'contact_group_id', 'Deal - Contact Group ID', 'integer'),
            (id_mask, 'SOURCE_ID', 'id', 'Deal - BU Database ID', 'integer'),
        ):
            for index in df.index[mask]:
                value = df.at[index, field]
                key = str(value).strip() if mode == 'serial' else int(value)
                audit.equal(index, kind, field, audit.original.at[index, field],
                            {key}, reference_source, [ref_field], mode)
    audit.titles(df['deal_title'], (
        ('PIPEDRIVE', deal_title_mask), ('ARCHIVE', archived_deal_title_mask),
    ))
    for index in df.index[c3_address_mask]:
        for key_column, columns in (
            ('combined_input_address', ('Input: Address', 'Input: City', 'Input: State')),
            ('combined_md_address', ('md_address', 'md_city', 'md_state')),
        ):
            audit.address(index, columns, df.at[index, key_column], 'C3')
    for index in df.index[c3_phone_mask]:
        for field in phone_columns:
            key = re.sub(r'\D', '', str(df.at[index, field]))
            audit.c3_equal(index, 'PHONE', field, audit.original.at[index, field], key)

    # Join all reasons with comma separator
    df['reason_for_removal'] = df['reason_for_removal'].apply(lambda lst: ', '.join(lst) if isinstance(lst, list) else lst)

    debug_df_state(df, "After joining reason_for_removal")
    print("Max reason_for_removal length:", df['reason_for_removal'].astype(str).str.len().max())
    # -------------------------------
    # Improved group-level conflict detection (order-independent)
    # -------------------------------

    CONFLICT_REASON = 'Conflicting group match – other record in same Contact Group ID found valid matches'

    # Force consistent type for grouping
    df['contact_group_id'] = pd.to_numeric(df['contact_group_id'], errors='coerce')
    debug_df_state(df, "After converting contact_group_id to numeric")
    
    # Define what counts as real matches
    SUCCESS_KEYWORDS = [
        'Phone Number is in Pipedrive',
        'Found matching Mailing Address from Pipedrive',
        'Found matching Deal Title from Pipedrive',
        'Serial Number is in Pipedrive',
        'Contact Group ID is in Pipedrive',
        'Bottoms Up ID is in Pipedrive',

        'Phone Number is in Pipedrive (Archived)',
        'Found matching Mailing Address from Pipedrive (Archived)',
        'Found matching Deal Title from Pipedrive (Archived)',
        'Serial Number is in Pipedrive (Archived)',
        'Contact Group ID is in Pipedrive (Archived)',
        'Bottoms Up ID is in Pipedrive (Archived)',

        'Found address in C3 table',
        'Phone Number is in C3 table'
    ]

    # Row has at least one real match
    df['has_real_match'] = df['reason_for_removal'].apply(
        lambda x: any(k in str(x) for k in SUCCESS_KEYWORDS)
    )

    # Row is ONLY manual review
    df['all_manual_review'] = df['reason_for_removal'].apply(
        lambda x: ('FOR MANUAL REVIEW' in str(x)) and not any(k in str(x) for k in SUCCESS_KEYWORDS)
    )

    # Group-level success detection (order doesn't matter)
    group_has_match = df.groupby('contact_group_id')['has_real_match'].transform('any')

    # Flag logic
    conflict_mask = df['all_manual_review'] & group_has_match
    debug_mask(conflict_mask, "Conflict rows flagged")

    # Append reason
    df.loc[conflict_mask, 'reason_for_removal'] = (
        df.loc[conflict_mask, 'reason_for_removal'] + ', ' + CONFLICT_REASON
    )
    debug_df_state(df, "After conflict tagging")

    # clean list tagging
    df['clean_tag'] = np.where(
        df['has_real_match'] | conflict_mask,
        'N',
        'Y'
    )

    # Cleanup helper cols
    df.drop(columns=['has_real_match', 'all_manual_review'], inplace=True)

    before_dedupe = len(df)
    print(f"\nBefore drop_duplicates: {before_dedupe:,} rows")

    if 'contact_group_id' in df.columns:
        print("Null contact_group_id rows before dedupe:", df['contact_group_id'].isna().sum())
        print("Duplicated contact_group_id rows before dedupe:", df['contact_group_id'].duplicated(keep=False).sum())
        

    df['_has_addr'] = has_mailing_address(df, BU_ADDRESS_SETS)
    with_cgid = (
        df[df['contact_group_id'].notna()]
        .sort_values('_has_addr', ascending=False, kind='stable')
        .drop_duplicates(subset=['contact_group_id'], keep='first')
        .sort_index()
        .copy()
    )
    without_cgid = df[df['contact_group_id'].isna()].copy()

    # Tag before the index reset below: audit rows are keyed by the original index.
    no_address_count = 0
    for frame in (with_cgid, without_cgid):
        no_address = frame['clean_tag'].eq('Y') & ~frame['_has_addr']
        frame.loc[no_address, 'clean_tag'] = 'N'
        frame.loc[no_address, 'reason_for_removal'] = (
            frame.loc[no_address, 'reason_for_removal'] + ', ' + NO_ADDRESS_REASON
        )
        for index in frame.index[no_address]:
            audit.add(index, 'NO_MAILING_ADDRESS', 'address', '', 'SOURCE_DATABASE', '', '')
        no_address_count += int(no_address.sum())
    print(f"Clean rows tagged N for no valid mailing address: {no_address_count:,}")

    df = pd.concat([with_cgid, without_cgid], ignore_index=True).drop(columns=['_has_addr'])

    after_dedupe = len(df)
    print(f"After drop_duplicates: {after_dedupe:,} rows")
    print(f"Rows removed by drop_duplicates: {before_dedupe - after_dedupe:,}")
    debug_df_state(df, "After drop_duplicates")

    # Capitalization of all names
    columns_to_transform = ['Owner', 'First Name', 'Middle Name', 'Last Name']
    df[columns_to_transform] = df[columns_to_transform].apply(lambda x: x.astype('string').str.upper())
    def upper_first(text):
        if pd.isna(text):
            return text
        words = text.split()
        return " ".join([word.capitalize() if not re.fullmatch(r'(?i)(i{1,3}|iv|v?i{0,3}|ix|x{1,3}|xl|l{1,3}|xc|c{1,3}|cd|d{1,3}|cm|m{1,4})', word) else word.upper() for word in words])
    df[columns_to_transform] = df[columns_to_transform].map(upper_first)
    df.drop(columns=['deal_title', 'md_norm', 'input_norm', 'combined_input_address', 'combined_md_address'], axis=1, inplace=True)
    # df.drop(columns=['deal_title'], axis=1, inplace=True)

    if audit.capped_matches:
        print(f'Match Details: capped {audit.capped_matches:,} heavily-shared match group(s) '
              f'to {MAX_EVIDENCE_IDS_PER_KEY} listed IDs each (plus an "and N more" note).')
    return audit.finish(df)

# Input adapters: stop on incomplete reference data instead of silently skipping it.
def require_columns(columns, required, label):
    missing = sorted(set(required) - set(columns))
    if missing:
        raise ValueError(f"{label}: missing columns: {', '.join(missing)}")


def read_table(path, required, label):
    path = Path(path)
    if path.suffix.lower() == '.csv':
        frame = pd.read_csv(path, dtype=str, encoding='utf-8-sig', low_memory=False)
    elif path.suffix.lower() in ('.xlsx', '.xls'):
        with pd.ExcelFile(path) as book:
            valid = [sheet for sheet in book.sheet_names if set(required).issubset(
                pd.read_excel(book, sheet_name=sheet, nrows=0).columns)]
            if not valid:
                raise ValueError(f'{label}: no worksheet has all required columns: {", ".join(required)}')
            frame = pd.concat([pd.read_excel(book, sheet_name=sheet, dtype=str) for sheet in valid], ignore_index=True)
    else:
        raise ValueError(f'{label}: select a CSV, XLSX, or XLS file.')
    require_columns(frame.columns, required, label)
    print(f'{label}: loaded {len(frame):,} rows.')
    return frame


def c3_files(folder):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError('Select an existing C3 folder.')
    result = {}
    for kind in ('Addresses', 'Emails', 'Phones'):
        matches = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith('~$')
                   and p.suffix.lower() in ('.csv', '.xlsx', '.xls')
                   and re.search(r'\b' + kind + r'\b', p.stem, flags=re.I)]
        if len(matches) != 1:
            raise ValueError(f'C3 needs exactly one {kind} file (CSV or Excel); found {len(matches)}. '
                             'Move older copies outside this folder, then check again.')
        result[kind] = matches[0]
    return result


def read_c3_with_row_refs(path):
    path = Path(path)
    required = ['OPT-OUT_MEDIUM', 'OPT-OUT_CONTACT']

    def mark_rows(frame, sheet):
        # Row 1 is the header; the first data row is row 2.
        label = f'{path.name} / {sheet}'.replace('|', '%7C')
        frame = frame.copy()
        frame['_c3_row_ref'] = [
            f'ROW: {label} / {number}'
            for number in range(2, len(frame) + 2)
        ]
        return frame

    if path.suffix.lower() == '.csv':
        frame = pd.read_csv(
            path,
            dtype=str,
            encoding='utf-8-sig',
            low_memory=False,
            skip_blank_lines=False,
        )
        require_columns(frame.columns, required, f'C3 {path.name}')
        return mark_rows(frame, 'CSV')

    if path.suffix.lower() in ('.xlsx', '.xls'):
        frames = []
        with pd.ExcelFile(path) as book:
            for sheet in book.sheet_names:
                headers = pd.read_excel(
                    book, sheet_name=sheet, nrows=0
                ).columns

                if set(required).issubset(headers):
                    frame = pd.read_excel(
                        book, sheet_name=sheet, dtype=str
                    )
                    frames.append(mark_rows(frame, sheet))

        if not frames:
            raise ValueError(
                f'C3 {path.name}: no worksheet has the required columns.'
            )

        return pd.concat(frames, ignore_index=True)

    raise ValueError('C3 files must be CSV, XLSX, or XLS.')


def load_c3(folder, address_normalizer=None):
    addresses, phones = set(), set()
    id_maps = {'ADDRESS': {}, 'PHONE': {}, 'PHONE_NUMERIC': {}, 'EMAIL': {}}
    for kind, path in c3_files(folder).items():
        frame = read_c3_with_row_refs(path)
        medium = frame['OPT-OUT_MEDIUM'].fillna('').str.strip()
        if not frame.empty and not medium.isin(['Direct Mail', 'Mailing Address', 'Phone', 'Email']).any():
            raise ValueError(f'C3 {kind}: no recognized OPT-OUT_MEDIUM values.')
        relevant = medium.isin(['Direct Mail', 'Mailing Address', 'Phone', 'Email']) & frame['OPT-OUT_CONTACT'].notna()
        contacts = frame.loc[relevant].copy()
        if not contacts.empty:
            columns = {
                str(c).strip().casefold(): c
                for c in contacts.columns
            }
            id_column = next(
                (
                    columns[name]
                    for name in ('c3_id', 'c3 id')
                    if name in columns
                ),
                None,
            )

            contacts['c3_id'] = (
                contacts[id_column].map(report_id)
                if id_column is not None
                else ''
            )

            missing_id = contacts['c3_id'].eq('')
            contacts.loc[missing_id, 'c3_id'] = contacts.loc[
                missing_id, '_c3_row_ref'
            ]

            if missing_id.any():
                print(
                    f'C3 {kind}: using source-row references for '
                    f'{missing_id.sum():,} contacts without c3_id.'
                )
        for _, row in contacts.iterrows():
            value = row['OPT-OUT_CONTACT']
            if row['OPT-OUT_MEDIUM'].strip() in ('Direct Mail', 'Mailing Address'):
                key = (address_normalizer(value) if address_normalizer is not None
                       else normalize_address(remove_trailing_zip(value)))
                if key:
                    addresses.add(key)
                    id_maps['ADDRESS'].setdefault(key, set()).add(row['c3_id'])
            elif row['OPT-OUT_MEDIUM'].strip() == 'Email':
                for key in campaign_email_tokens(value):
                    id_maps['EMAIL'].setdefault(key, set()).add(row['c3_id'])
            else:
                key = re.sub(r'\D', '', re.sub(r'\.0$', '', str(value).strip()))
                if key:
                    phones.add(key)
                    id_maps['PHONE'].setdefault(key, set()).add(row['c3_id'])
                    # NCOP/PO compare numeric phones; retain the corresponding ID key too.
                    id_maps['PHONE_NUMERIC'].setdefault(str(int(key)), set()).add(row['c3_id'])
    print(f"C3 ready: {len(addresses):,} addresses; {len(phones):,} phones; {len(id_maps['EMAIL']):,} emails, with c3_id links.")
    return C3MatchSets(addresses, phones, id_maps)


# C3 acquisition is shared by every population and does not change cleanup rules.
C3_DRIVE_NAME = re.compile(
    r'^(\d{2}-\d{2}-\d{4}) (Addresses|Phones|Emails) \(Consolidated\)\.xlsx$',
    re.IGNORECASE,
)
C3_XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
C3_SHEETS_MIME = 'application/vnd.google-apps.spreadsheet'


def c3_drive_folder_id(value):
    """Accept a Drive folder ID or its standard sharing URL."""
    value = value.strip()
    if re.fullmatch(r'[A-Za-z0-9_-]+', value):
        return value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == 'https' and parsed.hostname == 'drive.google.com':
        match = re.search(r'/folders/([A-Za-z0-9_-]+)', parsed.path)
        if match:
            return match.group(1)
        candidate = urllib.parse.parse_qs(parsed.query).get('id', [''])[0]
        if re.fullmatch(r'[A-Za-z0-9_-]+', candidate):
            return candidate
    raise ValueError('Enter the Google Drive C3 folder link or folder ID.')


def select_latest_c3_files(items):
    """Use the date in each filename, independently for each C3 kind."""
    candidates = {kind: [] for kind in ('Addresses', 'Phones', 'Emails')}
    for item in items:
        match = C3_DRIVE_NAME.fullmatch(item.get('name', ''))
        if not match or item.get('mimeType') not in (C3_XLSX_MIME, C3_SHEETS_MIME):
            continue
        try:
            date = datetime.strptime(match.group(1), '%m-%d-%Y')
        except ValueError:
            continue
        candidates[match.group(2).title()].append((date, item))
    selected = {}
    for kind, matches in candidates.items():
        if not matches:
            raise ValueError(f'Drive folder: no dated {kind} (Consolidated).xlsx file found.')
        newest = max(date for date, _ in matches)
        latest = [item for date, item in matches if date == newest]
        if len(latest) != 1:
            raise ValueError(f'Drive folder: multiple {kind} files dated {newest:%m-%d-%Y}. Keep one latest copy.')
        selected[kind] = latest[0]
    return selected


def _c3_drive_service(credentials_path):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from google_auth_httplib2 import AuthorizedHttp
        import httplib2
    except ImportError:
        raise ValueError('Install Google Drive support: pip install google-api-python-client google-auth-httplib2') from None
    if not credentials_path.is_file():
        raise ValueError(f'Service-account JSON not found: {credentials_path}')
    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_path), scopes=['https://www.googleapis.com/auth/drive.readonly'])
        return build('drive', 'v3', http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=60)),
                     cache_discovery=False)
    except Exception:
        raise ValueError('Could not initialize Google Drive. Check config/google_service_account.json and network access.') from None


def _download_c3_drive_file(service, item, target):
    from googleapiclient.http import MediaIoBaseDownload
    if item['mimeType'] == C3_SHEETS_MIME:
        request = service.files().export_media(fileId=item['id'], mimeType=C3_XLSX_MIME)
    else:
        request = service.files().get_media(fileId=item['id'], supportsAllDrives=True)
    print(f'Downloading C3 {target.name}...')
    with target.open('wb') as handle:
        downloader = MediaIoBaseDownload(handle, request, chunksize=4 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk(num_retries=3)
            if status:
                print(f'{target.name}: {status.progress():.0%} downloaded.')
    current = service.files().get(fileId=item['id'], supportsAllDrives=True,
                                  fields='modifiedTime,version,trashed').execute(num_retries=3)
    if current.get('trashed') or any(current.get(key) != item.get(key) for key in ('modifiedTime', 'version')):
        raise ValueError(f'{target.name} changed on Drive during download. Try again.')


def _replace_local_c3(staged, folder, backup):
    """Replace only recognized C3 data files; roll back ordinary write failures."""
    old_files = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith('~$')
                 and p.suffix.lower() in ('.csv', '.xlsx', '.xls')
                 and re.search(r'\b(Addresses|Phones|Emails)\b', p.stem, re.I)]
    # Refuse to overwrite an unrelated file or directory with the incoming name.
    for path in staged.iterdir():
        dest = folder / path.name
        if dest.exists() and dest not in old_files:
            raise ValueError(f'C3 destination already exists and is not a replaceable C3 file: {dest.name}')
    moved, installed = [], []
    try:
        for path in old_files:
            os.replace(path, backup / path.name)
            moved.append(path)
        for path in staged.iterdir():
            target = folder / path.name
            os.replace(path, target)
            installed.append(target)
    except OSError:
        try:
            for path in reversed(installed):
                path.unlink()
            for path in reversed(moved):
                os.replace(backup / path.name, path)
        except OSError:
            raise ValueError(f'C3 replacement and restoration could not finish. Previous files are in {backup}. Close files open in Excel and restore them before cleanup.') from None
        raise ValueError('Could not replace local C3 files. Previous files were restored. Close files open in Excel and try again.') from None


def c3_file_hash(path):
    import hashlib

    checksum = hashlib.sha256()

    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            checksum.update(block)

    return checksum.hexdigest()


def c3_remote_stamp(item):
    return {
        key: item.get(key)
        for key in ('id', 'name', 'mimeType', 'modifiedTime', 'version')
    }


def download_latest_c3(folder_link, destination, credentials_path=None):
    """Download and validate all three files before replacing local references.

    Searches the immediate Drive folder. Dates come from filenames, not upload
    order. Credentials default to config/google_service_account.json beside the
    scripts (or EXE). Remote Drive files are only read.
    """
    import tempfile
    import shutil
    folder_id = c3_drive_folder_id(folder_link)
    if not str(destination).strip():
        raise ValueError('Select a local C3 folder first.')
    folder = Path(destination).resolve()
    credentials_path = Path(credentials_path) if credentials_path else BASE / 'config' / 'google_service_account.json'
    service = _c3_drive_service(credentials_path)
    try:
        try:
            metadata = service.files().get(fileId=folder_id, fields='mimeType,driveId', supportsAllDrives=True).execute(num_retries=3)
            if metadata.get('mimeType') != 'application/vnd.google-apps.folder':
                raise ValueError('The Drive link must point to a folder.')
            items, token = [], None
            options = {'corpora': 'drive', 'driveId': metadata['driveId']} if metadata.get('driveId') else {'corpora': 'user'}
            while True:
                response = service.files().list(
                    q=f"'{folder_id}' in parents and trashed = false", pageSize=1000,
                    pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True,
                    fields='nextPageToken,incompleteSearch,files(id,name,mimeType,modifiedTime,version)',
                    **options).execute(num_retries=3)
                if response.get('incompleteSearch'):
                    raise ValueError('Drive returned an incomplete folder listing. Try again.')
                items.extend(response.get('files', []))
                token = response.get('nextPageToken')
                if not token:
                    break
            selected = select_latest_c3_files(items)
            for kind, item in selected.items():
                print(f'Latest C3 {kind}: {item["name"]}')
            folder.mkdir(parents=True, exist_ok=True)
            cache_path = folder / 'c3_drive_cache.json'

            try:
                cache = json.loads(
                    cache_path.read_text(encoding='utf-8')
                )

                if (
                    not isinstance(cache, dict)
                    or cache.get('folder_id') != folder_id
                    or not isinstance(cache.get('files'), dict)
                ):
                    cache = {}

            except (OSError, ValueError):
                cache = {}

            new_cache = {'folder_id': folder_id, 'files': {}}
            reused_count = 0
            downloaded_count = 0

            with tempfile.TemporaryDirectory(
                prefix='.c3-download-', dir=folder.parent
            ) as temp:
                staged = Path(temp)

                for kind, item in selected.items():
                    target = staged / item['name']
                    local = folder / item['name']
                    entry = cache.get('files', {}).get(kind, {})

                    if not isinstance(entry, dict):
                        entry = {}

                    stamp = c3_remote_stamp(item)
                    reused = False

                    if (
                        all(
                            value is not None
                            for value in stamp.values()
                        )
                        and entry.get('remote') == stamp
                        and local.is_file()
                    ):
                        shutil.copyfile(local, target)

                        reused = (
                            c3_file_hash(target)
                            == entry.get('sha256')
                        )

                    if reused:
                        reused_count += 1
                        print(
                            f'C3 {kind}: already current; '
                            'using local copy.'
                        )
                    else:
                        _download_c3_drive_file(
                            service, item, target
                        )
                        downloaded_count += 1

                    new_cache['files'][kind] = {
                        'remote': stamp,
                        'sha256': c3_file_hash(target),
                    }
                print('Validating downloaded C3 columns and data...')
                load_c3(staged)
                backup = Path(tempfile.mkdtemp(prefix='.c3-previous-', dir=folder.parent))
                try:
                    _replace_local_c3(staged, folder, backup)
                except Exception:
                    # Preserve any backup left behind if rollback itself failed.
                    if not any(backup.iterdir()):
                        backup.rmdir()
                    raise
                else:
                    shutil.rmtree(backup, ignore_errors=True)

                temporary_cache = Path(temp) / 'c3_drive_cache.json'
                temporary_cache.write_text(
                    json.dumps(new_cache, indent=2),
                    encoding='utf-8',
                )
                os.replace(temporary_cache, cache_path)

            print(
                f'C3 ready: {reused_count} reused; '
                f'{downloaded_count} downloaded. '
                'Latest Drive versions checked and local files validated.'
            )
            return {kind: str(folder / item['name']) for kind, item in selected.items()}
        except ValueError:
            raise
        except OSError:
            raise ValueError('Could not write C3 files. Check the local folder and close any files open in Excel.') from None
        except Exception as error:
            status = getattr(getattr(error, 'resp', None), 'status', None)
            suffix = f' (HTTP {status})' if status else ''
            raise ValueError('Google Drive download failed' + suffix + '. Check network access, enable the Drive API, and share the folder with the service-account email. Existing C3 files were kept if downloading or validation failed.') from None
    finally:
        service.close()


def open_db(path):
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != '.db':
        raise ValueError('Select the data source SQLite .db file.')
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def validate_db(path, category):
    if category not in CATEGORIES:
        raise ValueError('Select a category.')
    with contextlib.closing(open_db(path)) as con:
        require_columns(
            [r[1] for r in con.execute('PRAGMA table_info(bottoms_up)')],
            [c for c in BU_COLS if c != 'Input: Zip Code'],
            'bottoms_up table',
        )
        count = con.execute('SELECT COUNT(*) FROM bottoms_up WHERE Category = ?', (category,)).fetchone()[0]
    if not count:
        raise ValueError(f'There are no records with Category = {category}.')
    print(f'Database ready: {count:,} records in {category}. Source is read-only.')
    return count


def join_archive(archive, persons):
    persons = persons.copy()
    archive = with_reference_ids(archive, 'Deal - ID', ('ID', 'Deal ID'), 'Archived deals')
    archive = archive.copy().rename(columns={
        'Title': 'Deal - Title',
        'Contact Group ID': 'Deal - Contact Group ID',
        'Serial Number': 'Deal - Serial Number',
        'BU Database ID': 'Deal - BU Database ID',
        'NCOP ID': 'Deal - NCOP ID',
        'Pooling Order ID': 'Deal - Pooling Order ID',
        'Unique Database ID': 'Deal - Unique Database ID',
    })
    for df, col in ((archive, 'Contact person ID'), (persons, 'Person - ID')):
        df[col] = pd.to_numeric(df[col].replace('', pd.NA), errors='raise').astype('Int64')
    if persons['Person - ID'].isna().any() or persons['Person - ID'].duplicated().any():
        raise ValueError('Persons list contains missing or duplicate Person IDs.')
    person_columns = PERSON_COLS + [column for column in EMAIL_COLS if column in persons]
    merged = archive.merge(persons[person_columns], how='left', left_on='Contact person ID', right_on='Person - ID', validate='many_to_one')
    missing = merged['Contact person ID'].notna() & merged['Person - ID'].isna()
    if missing.any():
        print(f'NOTE: {missing.sum():,} archived rows have no person in filter 891899; their person address/phone/email checks are unavailable (same left-join behavior as original).')
    format_pipedrive_df(merged)
    merged['normalized_pipedrive_address'] = merged['Person - Mailing Address'].fillna('').apply(normalize_address)
    return merged


class Pipedrive:
    ROOT = f'https://{PIPEDRIVE_DOMAIN}/api/v2/'

    def __init__(self, token):
        self.token = token

    def get(self, endpoint, params):
        url = self.ROOT + endpoint + '?' + urllib.parse.urlencode(params)
        for attempt in range(5):
            request = urllib.request.Request(url, headers={'x-api-token': self.token, 'Accept': 'application/json'})
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    data = json.load(response)
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504):
                    raise ValueError(f'Pipedrive returned HTTP {error.code}. Check your API token and access to both filters.') from None
                if attempt == 4:
                    raise ValueError('Pipedrive is busy or its request limit was reached. Try again later.') from None
                try:
                    delay = max(1, float(error.headers.get('Retry-After', 2 ** attempt)))
                except ValueError:
                    delay = 2 ** attempt
                if delay > 60:
                    raise ValueError('Pipedrive requested a longer wait. Try again in a few minutes.') from None
                print(f'Pipedrive is busy. Retrying in {delay:g} seconds...')
                time.sleep(delay)
                continue
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 4:
                    raise ValueError('Pipedrive connection failed. Check your internet and try again.') from None
                time.sleep(2 ** attempt)
                continue
            except ValueError:
                raise ValueError('Pipedrive returned an unreadable response.') from None
            if data.get('success') is not True:
                raise ValueError('Pipedrive could not complete this request.')
            return data
        raise ValueError('Pipedrive request failed.')

    def all(self, endpoint, **params):
        rows, seen = [], set()
        params = dict(params, limit=500)
        while True:
            payload = self.get(endpoint, params)
            page = payload.get('data') or []
            if not isinstance(page, list):
                raise ValueError('Unexpected Pipedrive response structure.')
            rows.extend(page)
            print(f'Pipedrive {endpoint}: {len(rows):,} records downloaded...')
            cursor = (payload.get('additional_data') or {}).get('next_cursor')
            if not cursor:
                break
            if cursor in seen:
                raise ValueError('Pipedrive pagination repeated a page; download stopped.')
            seen.add(cursor)
            params['cursor'] = cursor
        return rows

    def fields(self, endpoint, names):
        fields = self.all(endpoint)
        result = {}
        for name in names:
            matches = [f for f in fields if str(f.get('field_name', f.get('name', ''))).strip().casefold() == name.casefold()]
            if len(matches) != 1:
                raise ValueError(f'Pipedrive field "{name}" is missing or ambiguous. Use manual exports or ask the maintainer to check field names.')
            field = matches[0]
            key = field.get('field_code') or field.get('key')
            if not key:
                raise ValueError(f'Pipedrive returned no field code for {name}.')
            result[name] = key
        return result


def custom_value(item, key):
    fields = item.get('custom_fields') or {}
    if key not in fields and key not in item:
        raise ValueError('A required Pipedrive custom field was omitted. Check field visibility or use manual exports.')
    value = fields.get(key, item.get(key))
    if isinstance(value, dict):
        if 'value' not in value:
            raise ValueError('Unexpected custom field format from Pipedrive.')
        value = value['value']
    if isinstance(value, (list, dict)):
        raise ValueError('This cleanup expects text/numeric custom fields. Use manual exports for this field type.')
    return value


def person_row(person, address_key):
    row = dict.fromkeys(PERSON_COLS + EMAIL_COLS, '')
    row['Person - ID'] = person['id']
    row['Person - Name'] = person.get('name') or ''
    row['Person - Mailing Address'] = custom_value(person, address_key)
    numbers = person.get('phones', person.get('phone', [])) or []
    for number in numbers:
        label = str(number.get('label') or 'other').title()
        if label not in ('Work', 'Home', 'Mobile', 'Other'):
            label = 'Other'
        col = 'Person - Phone - ' + label
        value = str(number.get('value') or '')
        row[col] = ','.join(filter(None, (row[col], value)))
    for email in person.get('emails', person.get('email', [])) or []:
        label = str(email.get('label') or 'other').strip().title()
        if label not in ('Home', 'Work', 'Other'):
            label = 'Other'
        column = 'Person - Email - ' + label
        value = str(email.get('value') or '')
        row[column] = ','.join(filter(None, (row[column], value)))
    return row



def download_pipedrive(token):
    api = Pipedrive(token)
    try:
        deal_keys = api.fields(
            'dealFields',
            ['Contact Group ID', 'Serial Number', 'BU Database ID', 'NCOP ID', 'Pooling Order ID', 'Unique Database ID'],
        )
        address_key = api.fields('personFields', ['Mailing Address'])['Mailing Address']
        deals = api.all('deals', filter_id=366502, custom_fields=','.join(deal_keys.values()))
        persons = api.all('persons', filter_id=891899, custom_fields=address_key)
        if not deals or not persons:
            raise ValueError('A Pipedrive filter returned no records. Confirm filter access before proceeding.')
        people = {int(p['id']): p for p in persons}
        if len(people) != len(persons):
            raise ValueError('Pipedrive returned duplicate persons. Refresh the download.')
        persons_frame = pd.DataFrame(
            [person_row(p, address_key) for p in persons],
            columns=PERSON_COLS + EMAIL_COLS + ['Person - Name'],
        )
        # Deal exports contain associated person data even when that person is outside the persons filter.
        wanted = {int(d['person_id']) for d in deals if d.get('person_id') is not None}
        missing = sorted(wanted - set(people))
        for start in range(0, len(missing), 100):
            extra = api.all('persons', ids=','.join(map(str, missing[start:start+100])), custom_fields=address_key)
            people.update({int(p['id']): p for p in extra})
        unresolved = wanted - set(people)

        if unresolved:
            print(
                f'{len(unresolved):,} linked contacts were not returned. '
                'Keeping their deals; unavailable person addresses '
                'and phones will be blank.'
            )
        rows = []
        for deal in deals:
            row = {'Deal - Title': deal.get('title'), 'Deal - ID': report_id(deal['id'])}
            row.update({'Deal - ' + name: custom_value(deal, key) for name, key in deal_keys.items()})
            person_id = deal.get('person_id')
            person = (
                people.get(int(person_id))
                if person_id is not None
                else None
            )

            if person is not None:
                row.update(person_row(person, address_key))
            else:
                row.update(dict.fromkeys(PERSON_COLS + EMAIL_COLS, ''))
                row['Person - ID'] = (
                    person_id if person_id is not None else ''
                )
            rows.append(row)
        return pd.DataFrame(rows, columns=DEAL_COLS + PERSON_COLS + EMAIL_COLS), persons_frame
    finally:
        api.token = ''


def fingerprints(config):
    campaign = is_campaign_population(config['population'])
    paths = [Path(config['archive']), *c3_files(config['c3']).values()]
    if campaign:
        if config.get('population_mode') == 'Manual files':
            paths.insert(0, Path(config['population_file']))
    else:
        paths.insert(0, Path(config['db']))
    if config['mode'] == 'Manual files':
        paths.extend([Path(config['deals']), Path(config['persons'])])
    result = []
    for path in paths:
        stat = path.stat()
        result.append((str(path.resolve()), stat.st_size, stat.st_mtime_ns))
    # SQLite may be updated through a write-ahead log without changing the main file.
    if not campaign:
        wal = Path(config['db'] + '-wal')
        result.append((str(wal), wal.stat().st_size, wal.stat().st_mtime_ns) if wal.exists() else (str(wal), None, None))
    return result


def load_or_download_pipedrive(token):
    import hashlib
    import tempfile

    max_age = 8 * 60 * 60

    # Separate caches by account, filters, and API token.
    # The token itself is never saved.
    scope = f'{Pipedrive.ROOT}|366502|891899|cache-v7-person-name|{token}'


    cache_id = hashlib.sha256(scope.encode('utf-8')).hexdigest()

    folder = BASE / 'pipedrive_cache' / cache_id
    folder.mkdir(parents=True, exist_ok=True)

    metadata_path = folder / 'metadata.json'
    filenames = (
        'pipedrive_deals_366502.csv',
        'pipedrive_persons_891899.csv',
    )
    required = (DEAL_COLS + PERSON_COLS + EMAIL_COLS, PERSON_COLS + EMAIL_COLS + ['Person - Name'],)
    def digest(handle):
        checksum = hashlib.sha256()

        for block in iter(lambda: handle.read(1024 * 1024), b''):
            checksum.update(block)

        handle.seek(0)
        return checksum.hexdigest()

    def mark_download_time(frames, downloaded_at):
        for frame in frames:
            frame.attrs['pipedrive_downloaded_at'] = downloaded_at

        return tuple(frames)

    # Try using the existing local files first.
    try:
        metadata = json.loads(
            metadata_path.read_text(encoding='utf-8')
        )

        downloaded_at = float(metadata['downloaded_at'])
        age = time.time() - downloaded_at

        if not 0 <= age < max_age:
            raise ValueError('The saved downloads have expired.')

        frames = []

        for name, columns in zip(filenames, required):
            with (folder / name).open('rb') as handle:
                if digest(handle) != metadata['files'][name]['sha256']:
                    raise ValueError(
                        'A saved file changed or is incomplete.'
                    )

                frame = pd.read_csv(
                    handle,
                    dtype=str,
                    keep_default_na=False,
                    encoding='utf-8-sig',
                    low_memory=False,
                )

            require_columns(frame.columns, columns, name)

            if (
                frame.empty
                or len(frame) != metadata['files'][name]['rows']
            ):
                raise ValueError(
                    'A saved file has an unexpected row count.'
                )

            frames.append(frame)

        # Check again in case reading large files crossed the expiry time.
        if not 0 <= time.time() - downloaded_at < max_age:
            raise ValueError(
                'The saved downloads expired while loading.'
            )

        print(
            f'Using saved Pipedrive files '
            f'({age / 3600:.2f} hours old). '
            'No API download needed.'
        )

        return mark_download_time(frames, downloaded_at)

    except (OSError, ValueError, KeyError, TypeError):
        print(
            'No complete Pipedrive cache under 8 hours old. '
            'Downloading both lists...'
        )

    # Download both lists using your existing API function.
    frames = download_pipedrive(token)
    downloaded_at = time.time()

    metadata = {
        'downloaded_at': downloaded_at,
        'files': {},
    }

    # Finish writing both files before marking the cache as complete.
    with tempfile.TemporaryDirectory(dir=folder) as temporary:
        temporary = Path(temporary)

        for name, frame, columns in zip(
            filenames, frames, required
        ):
            require_columns(frame.columns, columns, name)

            if frame.empty:
                raise ValueError(
                    f'{name} is empty; cache was not saved.'
                )

            path = temporary / name
            frame.to_csv(
                path,
                index=False,
                encoding='utf-8-sig',
            )

            with path.open('rb') as handle:
                checksum = digest(handle)

            metadata['files'][name] = {
                'sha256': checksum,
                'rows': len(frame),
            }

        (temporary / 'metadata.json').write_text(
            json.dumps(metadata, indent=2),
            encoding='utf-8',
        )

        for name in filenames:
            os.replace(temporary / name, folder / name)

        # Metadata is replaced last to detect interrupted saves.
        os.replace(
            temporary / 'metadata.json',
            metadata_path,
        )

    print(
        f'Both Pipedrive files saved for reuse for 8 hours: '
        f'{folder}'
    )

    return mark_download_time(frames, downloaded_at)


# NCOP uses the shared references, but has no category or contact-group rules.
NCOP_SCOPES = {
    'Original Owners': ('Y',),
    'Heirs': ('N',),
    'Both': ('Y', 'N'),
}
NCOP_ADDRESS_SETS = {
    'address_set_1': ('address', 'city', 'state'),
    'address_set_2': ('address2', 'city2', 'state2'),
    'address_set_3': ('add_address1', 'add_address1_city', 'add_address1_state'),
}
NCOP_PHONE_COLS = (
    [f'cleaned_phone{i}' for i in range(1, 5)]
    + [f'phone_{i}' for i in range(1, 6)]
)
NCOP_COLS = (
    ['ncop_id', 'is_original', 'contact_type', 'deal_id', 'serial_number',
     'first_name', 'last_name', 'owner_name', 'combined_county']
    + [column for columns in NCOP_ADDRESS_SETS.values() for column in columns]
    + NCOP_PHONE_COLS
)


def ncop_text(value):
    if pd.isna(value):
        return ''
    text = str(value).strip()
    return '' if text.upper() in ('', 'NAN', '<NA>', 'NONE', 'NULL') else text


NO_ADDRESS_REASON = 'No valid mailing address (street, city, state)'
ADDRESS_PLACEHOLDERS = {'N/A', 'NA', 'UNKNOWN'}
BU_ADDRESS_SETS = (
    ('Input: Address', 'Input: City', 'Input: State'),
    ('md_address', 'md_city', 'md_state'),
)


def address_part(value):
    text = ncop_text(value)
    return '' if text.upper() in ADDRESS_PLACEHOLDERS else text


def has_mailing_address(frame, address_sets):
    # True per row when any one address set has street, city and state all filled.
    mask = pd.Series(False, index=frame.index)
    for columns in address_sets:
        complete = pd.Series(True, index=frame.index)
        for column in columns:
            complete &= frame[column].map(address_part).ne('')
        mask |= complete
    return mask


def count_no_address(result):
    return int(result['reason_for_removal'].astype(str).str.contains(NO_ADDRESS_REASON, regex=False).sum())


def ncop_selection_sql(scope):
    if scope not in NCOP_SCOPES:
        raise ValueError('Select Original Owners, Heirs, or Both for NCOP.')
    values = NCOP_SCOPES[scope]
    placeholders = ','.join('?' for _ in values)
    # Retain the old contact-type restrictions; remove the hardcoded ID list.
    where = (
        f'UPPER(TRIM(is_original)) IN ({placeholders}) '
        "AND contact_type IS NOT NULL "
        "AND UPPER(TRIM(contact_type)) NOT IN ('COMPANY', 'RELIGIOUS INSTITUTION')"
    )
    return where, values


def validate_ncop_db(path, scope):
    where, values = ncop_selection_sql(scope)
    with contextlib.closing(open_db(path)) as con:
        columns = [row[1] for row in con.execute('PRAGMA table_info(ncop)')]
        require_columns(columns, NCOP_COLS, 'ncop table')
        count = con.execute(
            f'SELECT COUNT(*) FROM ncop WHERE {where}', values
        ).fetchone()[0]
    if not count:
        raise ValueError(f'No eligible NCOP records found for {scope}.')
    print(f'NCOP ready: {count:,} rows for {scope}. Source is read-only.')
    return count


def reference_columns(population):
    if is_campaign_population(population):
        return (
            ['Deal - ID'] + [field for field, *_ in CAMPAIGN_ID_CHECKS]
            + ['Person - Mailing Address'] + PHONE_COLS + EMAIL_COLS,
            ['ID', 'Contact person ID']
            + [field.removeprefix('Deal - ') for field, *_ in CAMPAIGN_ID_CHECKS],
        )
    if population == 'NCOP':
        return (
            ['Deal - Title', 'Deal - Serial Number', 'Deal - NCOP ID']
            + PERSON_COLS[1:],
            ['Title', 'Serial Number', 'NCOP ID', 'Contact person ID'],
        )
    if population == 'Pooling Order':
        return (
            ['Deal - Title', 'Deal - Serial Number', 'Deal - Pooling Order ID']
            + PERSON_COLS[1:],
            ['Title', 'Serial Number', 'Pooling Order ID', 'Contact person ID'],
        )
    if population == 'NTM or Non-Targeted Marketing':
        return (
            ['Deal - Title', 'Deal - Serial Number', 'Deal - Unique Database ID']
            + PERSON_COLS[1:],
            ['Title', 'Serial Number', 'Unique Database ID', 'Contact person ID'],
        )
    return (
        [column for column in DEAL_COLS
         if column not in ('Deal - NCOP ID', 'Deal - Pooling Order ID', 'Deal - ID', 'Deal - Unique Database ID')]
        + PERSON_COLS[1:],
        ARCHIVE_COLS,
    )


def ncop_phone_tokens(value):
    numbers = set()
    for token in re.split(r'[,;|]', ncop_text(value)):
        digits = re.sub(r'\D', '', re.sub(r'\.0$', '', token.strip()))
        if digits:
            numbers.add(int(digits))
    return numbers


def ncop_titles(row):
    names = {ncop_text(row['owner_name'])}
    first = ncop_text(row['first_name'])
    last = ncop_text(row['last_name'])
    if first and last:
        names.add(f'{first} {last}')

    locations = set()

    for entry in ncop_text(row['combined_county']).split('|'):
        entry = re.sub(r'\s+', ' ', entry).strip()
        if not entry:
            continue

        # Each entry contains one county and its corresponding state.
        county, separator, state = entry.rpartition(',')
        county = county.strip().upper()
        state = state.strip().upper()

        # Avoid adding "County" twice.
        county = re.sub(r'\s+COUNTY$', '', county).strip()

        if (
            not separator
            or not county
            or not re.fullmatch(r'[A-Z]{2}', state)
        ):
            raise ValueError(
                f"NCOP {row.get('ncop_id', '')}: invalid combined_county "
                f"entry {entry!r}. Expected 'County, ST' separated by pipes."
            )

        locations.add((county, state))

    return sorted({
        f'{name} {county} County, {state}'
        for name in names
        if name
        for county, state in locations
    })


def cleanup_ncop(df, ready):
    result = df.copy().reset_index(drop=True)
    audit = MatchEvidence('NCOP', result, {
        'PIPEDRIVE': ready['deals'].copy(), 'ARCHIVE': ready['archived'].copy(),
    }, ready['c3'])
    for reference in audit.references.values():
        format_pipedrive_df(reference)
    reasons = {index: [] for index in result.index}
    result['ncop_deal_id_equivalent'] = result['deal_id'].map(ncop_text)
    assigned = result['ncop_deal_id_equivalent'].ne('')
    for index in result.index[assigned]:
        reasons[index].append('Existing Deal ID in NCOP: ' + result.at[index, 'ncop_deal_id_equivalent'])
        audit.existing(index)
    # These rows are excluded first; only unassigned rows enter matching.
    pending = result.loc[~assigned].copy()
    print(f'NCOP: {assigned.sum():,} existing Deal IDs; {len(pending):,} rows need matching.')
    for column in NCOP_ADDRESS_SETS:
        result[column] = ''
    result['count_of_distinct_address'] = pd.Series(pd.NA, index=result.index, dtype='Int64')
    result['deal_title_match'] = ''
    if not pending.empty:
        lookups = {}
        for label, source in (('Pipedrive', ready['deals']), ('Archived Pipedrive', ready['archived'])):
            reference = source.copy()
            format_pipedrive_df(reference)
            ids = {
                number
                for value in reference['Deal - NCOP ID'].dropna()
                for token in split_pipe_values(value)
                if (number := extract_int_token(token)) is not None
            }
            lookups[label] = {
                'addresses': set(reference['Person - Mailing Address'].map(normalize_address)) - {''},
                'phones': get_phone_set(reference),
                'serials': get_serial_set(reference),
                'ids': ids,
            }
        c3_addresses, raw_c3_phones = ready['c3']
        c3_phones = {number for value in raw_c3_phones for number in ncop_phone_tokens(value)}
        # Keep NCOP's name/county title variations, using the shared substring matcher.
        titles = pending.apply(ncop_titles, axis=1).explode().dropna()
        current_title, archived_title = build_deal_title_masks(
            titles, ready['deals']['Deal - Title'], ready['archived']['Deal - Title']
        )
        audit.titles(titles, (('PIPEDRIVE', current_title), ('ARCHIVE', archived_title)))
        for label, mask in (('Pipedrive', current_title), ('Archived Pipedrive', archived_title)):
            for index in mask[mask].index.unique():
                reason = f'Found matching Deal Title from {label}'
                reasons[index].append(reason)
                previous = result.at[index, 'deal_title_match']
                result.at[index, 'deal_title_match'] = '; '.join(filter(None, (previous, reason)))
        for number, (index, row) in enumerate(pending.iterrows(), 1):
            distinct = set()
            for output_column, columns in NCOP_ADDRESS_SETS.items():
                address = ', '.join(ncop_text(row[column]) for column in columns if ncop_text(row[column]))
                address = normalize_address(remove_trailing_zip(address))
                matches = []
                if address:
                    distinct.add(address)
                    for label, lookup in lookups.items():
                        if address in lookup['addresses']:
                            matches.append(f'Address found in {label}')
                            audit.address(index, columns, address,
                                          'PIPEDRIVE' if label == 'Pipedrive' else 'ARCHIVE')
                    if address in c3_addresses:
                        matches.append('Address found in C3')
                        audit.address(index, columns, address, 'C3')
                result.at[index, output_column] = '; '.join(matches)
                reasons[index].extend(f'{output_column}: {match}' for match in matches)
            result.at[index, 'count_of_distinct_address'] = len(distinct)
            phones = {phone for column in NCOP_PHONE_COLS for phone in ncop_phone_tokens(row[column])}
            serials = set(split_pipe_values(row['serial_number']))
            source_id = int(row['ncop_id'])
            for label, lookup in lookups.items():
                audit.contact_values(
                    index, row, NCOP_PHONE_COLS,
                    'PIPEDRIVE' if label == 'Pipedrive' else 'ARCHIVE',
                    bool(phones & lookup['phones']),
                    bool(serials & lookup['serials']), source_id in lookup['ids'],
                )
                if phones & lookup['phones']:
                    reasons[index].append(f'Phone Number is in {label}')
                if serials & lookup['serials']:
                    reasons[index].append(f'Serial Number is in {label}')
                if source_id in lookup['ids']:
                    reasons[index].append(f'NCOP ID is in {label}')
            if phones & c3_phones:
                reasons[index].append('Phone Number is in C3')
                audit.contact_values(index, row, NCOP_PHONE_COLS, 'C3', True)
            if number % 1000 == 0 or number == len(pending):
                print(f'NCOP matching: {number:,} / {len(pending):,} rows...')
    has_address = has_mailing_address(result, NCOP_ADDRESS_SETS.values())
    for index in result.index:
        if not reasons[index] and not has_address.at[index]:
            reasons[index].append(NO_ADDRESS_REASON)
            audit.add(index, 'NO_MAILING_ADDRESS', 'address', '', 'SOURCE_DATABASE', '', '')
    result['clean_tag'] = [
        'N' if reasons[index] else 'Y'
        for index in result.index
    ]
    result['reason_for_removal'] = [
        '; '.join(reasons[index])
        if reasons[index]
        else 'No cleanup matches found - FOR MANUAL REVIEW'
        for index in result.index
    ]

    # Remove legacy aliases, including any inherited from the source.
    result = result.drop(
        columns=['clean', 'summary_findings'],
        errors='ignore',
    )
    return audit.finish(result)


def execute_ncop(config, ready, result_folder):
    where, values = ncop_selection_sql(config['ncop_scope'])
    with contextlib.closing(open_db(config['db'])) as con:
        chunks = []
        loaded = 0
        for chunk in pd.read_sql_query(
            f'SELECT * FROM ncop WHERE {where} ORDER BY ncop_id', con,
            params=values, chunksize=25000,
        ):
            chunks.append(chunk)
            loaded += len(chunk)
            print(f'Reading NCOP: {loaded:,} / {ready["count"]:,} rows...')
    if not chunks:
        raise ValueError('No NCOP rows remain. Check references again.')
    if ready['fingerprints'] != fingerprints(config):
        raise ValueError('The database changed while reading. Check references again.')
    df = pd.concat(chunks, ignore_index=True)
    df['ncop_id'] = pd.to_numeric(df['ncop_id'], errors='raise').astype('Int64')
    if df['ncop_id'].isna().any():
        raise ValueError('NCOP contains a missing ncop_id.')
    result = cleanup_ncop(df, ready)
    date = datetime.now(ZoneInfo('America/Chicago')).strftime('%Y%m%d')
    outputs = []
    for flag in NCOP_SCOPES[config['ncop_scope']]:
        label = 'Original Owners' if flag == 'Y' else 'Heirs'
        subset = result.loc[result['is_original'].map(ncop_text).str.upper().eq(flag)]
        # A selected owner type gets an output file, even when it has zero rows.
        for part, start in enumerate(range(0, max(1, len(subset)), 1048575), 1):
            suffix = f' - part {part}' if len(subset) > 1048575 else ''
            target = result_folder / f'{date} - NCOP - {label} - With Cleanup Tagging{suffix}.xlsx'
            with pd.ExcelWriter(target, engine='xlsxwriter', engine_kwargs={
                'options': {'strings_to_formulas': False, 'strings_to_urls': False, 'constant_memory': True, 'use_zip64': True}
            }) as writer:
                write_cleanup_tabs(writer, result, subset.iloc[start:start+1048575])
            outputs.append(target.name)
            print(f'Saved {target.name}: {len(subset.iloc[start:start+1048575]):,} rows.')
    summary = {
        'population': 'NCOP', 'owner_selection': config['ncop_scope'],
        'input_rows': loaded, 'output_rows': len(result),
        'clean_Y': int(result['clean_tag'].eq('Y').sum()),
        'excluded_N': int(result['clean_tag'].eq('N').sum()),
        'no_mailing_address_N': count_no_address(result),
        'group_duplicates_removed': 0, 'files': outputs,
        'reference_checked_at': ready['prepared_at'],
        'completed_at': datetime.now().isoformat(timespec='seconds'),
        'source_files': ready['fingerprints'], 'pipedrive_mode': config['mode'],
        'deal_filter': 366502, 'person_filter': 891899,
    }
    (result_folder / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    if config['mode'] == 'Download via API':
        ready['deals'].to_csv(result_folder / 'pipedrive_deals_366502.csv', index=False)
        ready['persons'].to_csv(result_folder / 'pipedrive_persons_891899.csv', index=False)
    print(f'COMPLETE: {len(result):,} NCOP rows | Y: {summary["clean_Y"]:,} | N: {summary["excluded_N"]:,} (no mailing address: {summary["no_mailing_address_N"]:,})')
    return summary


# Pooling Order has no category, owner-scope filter, grouping, or deduplication.
PO_COUNTY_COL = 'target_county'
PO_STATE_COL = 'target_state'
PO_ADDRESS_SETS = {
    'address_set_1': ('address', 'city', 'state'),
    'address_set_2': ('add_address1', 'add_address1_city', 'add_address1_state'),
}
PO_PHONE_COLS = [f'ph_phone{i}' for i in range(1, 6)]
PO_COLS = (
    [
        'po_id', 'contact_type', 'deal_id', 'serial_number',
        'lessor_owner', 'first_name', 'last_name', PO_COUNTY_COL, PO_STATE_COL,
    ]
    + [column for columns in PO_ADDRESS_SETS.values() for column in columns]
    + PO_PHONE_COLS
)
# Retain NCOP's contact-type eligibility rules.
PO_WHERE = (
    "contact_type IS NOT NULL AND UPPER(TRIM(contact_type)) "
    "NOT IN ('COMPANY', 'RELIGIOUS INSTITUTION')"
)


def normalize_po_address(value):
    text = ncop_text(value).upper().strip(' ,')
    # Repeat because references may end with either ZIP + USA or USA + ZIP.
    for _ in range(2):
        text = re.sub(r'(?:,\s*|\s+)(?:USA|U\.?S\.?A\.?)$', '', text).strip(' ,')
        text = remove_trailing_zip(text).strip(' ,')
    return normalize_address(text)


def po_id_tokens(value):
    # Preserve the complete text ID, including prefix and leading zeros.
    return {token.strip() for token in split_pipe_values(value) if ncop_text(token)}


def validate_po_db(path, _selected_filter=None):
    with contextlib.closing(open_db(path)) as con:
        columns = [row[1] for row in con.execute('PRAGMA table_info(pooling_order)')]
        require_columns(columns, PO_COLS, 'pooling_order table')
        count = con.execute(
            f'SELECT COUNT(*) FROM pooling_order WHERE {PO_WHERE}'
        ).fetchone()[0]
    if not count:
        raise ValueError('No eligible Pooling Order records found.')
    print(f'Pooling Order ready: {count:,} rows. Source is read-only.')
    return count


# Keep multiword county names together when spaces separate counties.
# Add other multiword names present in your PO data.
PO_MULTIWORD_COUNTIES = (
    'SAN AUGUSTINE',
    'SAN JACINTO',
    'SAN PATRICIO',
    'SAN SABA',
    'EL PASO',
    'DE WITT',
    'LA SALLE',
    'RED RIVER',
    'JEFF DAVIS',
    'TOM GREEN',
    'PALO PINTO',
    'LIVE OAK',
    'ROGER MILLS',
)


def split_po_counties(value):
    text = re.sub(r'\s+', ' ', ncop_text(value).upper()).strip()
    if not text:
        return []

    # Remove descriptive suffixes before splitting.
    text = re.sub(r'\bCOUNT(?:Y|IES)\b', '', text)
    text = re.sub(r'\s+', ' ', text).strip()

    protected = {}
    for number, county in enumerate(
        sorted(PO_MULTIWORD_COUNTIES, key=len, reverse=True)
    ):
        marker = f'__COUNTY_{number}__'
        pattern = r'\b' + re.escape(county) + r'\b'
        if re.search(pattern, text):
            text = re.sub(pattern, marker, text)
            protected[marker] = county

    counties = []
    for token in re.split(r'[\s&,]+', text):
        county = protected.get(token, token).strip()
        if county and county not in counties:
            counties.append(county)

    return counties


def po_titles(row):
    counties = split_po_counties(row[PO_COUNTY_COL])
    state = ncop_text(row[PO_STATE_COL]).upper()

    names = {ncop_text(row['lessor_owner'])}

    first = ncop_text(row.get('first_name'))
    last = ncop_text(row.get('last_name'))
    if first and last:
        names.add(f'{first} {last}')

    if not counties or not state:
        return []

    return sorted({
        f'{name} {county} County, {state}'
        for name in names
        if name
        for county in counties
    })


def cleanup_po(df, ready):
    result = df.copy().reset_index(drop=True)
    audit = MatchEvidence('Pooling Order', result, {
        'PIPEDRIVE': ready['deals'].copy(), 'ARCHIVE': ready['archived'].copy(),
    }, ready['c3'])
    reasons = {index: [] for index in result.index}
    assigned_ids = result['deal_id'].map(ncop_text)
    assigned = assigned_ids.ne('')
    for index in result.index[assigned]:
        reasons[index].append('Existing Deal ID in PO: ' + assigned_ids.at[index])
        audit.existing(index)
    pending = result.loc[~assigned]
    print(f'PO: {assigned.sum():,} existing Deal IDs; {len(pending):,} rows need matching.')
    for column in PO_ADDRESS_SETS:
        result[column] = ''
    result['count_of_distinct_address'] = pd.Series(pd.NA, index=result.index, dtype='Int64')
    result['deal_title_match'] = ''
    if not pending.empty:
        lookups = {}
        for label, reference in (
            ('Pipedrive', ready['deals']),
            ('Archived Pipedrive', ready['archived']),
        ):
            lookups[label] = {
                'addresses': set(reference['Person - Mailing Address'].map(normalize_po_address)) - {''},
                'phones': get_phone_set(reference),
                'serials': get_serial_set(reference),
                'ids': {
                    token for value in reference['Deal - Pooling Order ID']
                    for token in po_id_tokens(value)
                },
            }
        c3_addresses, raw_c3_phones = ready['c3']
        c3_phones = {number for value in raw_c3_phones for number in ncop_phone_tokens(value)}
        titles = pending.apply(po_titles, axis=1).explode().dropna()
        current_mask, archived_mask = build_deal_title_masks(
            titles, ready['deals']['Deal - Title'], ready['archived']['Deal - Title']
        )
        audit.titles(titles, (('PIPEDRIVE', current_mask), ('ARCHIVE', archived_mask)))
        for label, mask in (('Pipedrive', current_mask), ('Archived Pipedrive', archived_mask)):
            for index in mask[mask].index.unique():
                reason = f'Found matching Deal Title from {label}'
                reasons[index].append(reason)
                previous = result.at[index, 'deal_title_match']
                result.at[index, 'deal_title_match'] = '; '.join(filter(None, (previous, reason)))
        for number, (index, row) in enumerate(pending.iterrows(), 1):
            distinct = set()
            for output_column, columns in PO_ADDRESS_SETS.items():
                parts = [ncop_text(row[column]) for column in columns]
                address = normalize_po_address(', '.join(part for part in parts if part))
                matches = []
                if address:
                    distinct.add(address)
                    for label, lookup in lookups.items():
                        if address in lookup['addresses']:
                            matches.append(f'Address found in {label}')
                            audit.address(index, columns, address,
                                          'PIPEDRIVE' if label == 'Pipedrive' else 'ARCHIVE')
                    if address in c3_addresses:
                        matches.append('Address found in C3')
                        audit.address(index, columns, address, 'C3')
                result.at[index, output_column] = '; '.join(matches)
                reasons[index].extend(f'{output_column}: {match}' for match in matches)
            result.at[index, 'count_of_distinct_address'] = len(distinct)
            phones = {phone for column in PO_PHONE_COLS for phone in ncop_phone_tokens(row[column])}
            serials = set(split_pipe_values(row['serial_number']))
            source_id = ncop_text(row['po_id'])
            for label, lookup in lookups.items():
                audit.contact_values(
                    index, row, PO_PHONE_COLS,
                    'PIPEDRIVE' if label == 'Pipedrive' else 'ARCHIVE',
                    bool(phones & lookup['phones']),
                    bool(serials & lookup['serials']), bool(source_id) and source_id in lookup['ids'],
                )
                if phones & lookup['phones']:
                    reasons[index].append(f'Phone Number is in {label}')
                if serials & lookup['serials']:
                    reasons[index].append(f'Serial Number is in {label}')
                if source_id and source_id in lookup['ids']:
                    reasons[index].append(f'Pooling Order ID is in {label}')
            if phones & c3_phones:
                reasons[index].append('Phone Number is in C3')
                audit.contact_values(index, row, PO_PHONE_COLS, 'C3', True)
            if number % 1000 == 0 or number == len(pending):
                print(f'PO matching: {number:,} / {len(pending):,} rows...')
    has_address = has_mailing_address(result, PO_ADDRESS_SETS.values())
    for index in result.index:
        if not reasons[index] and not has_address.at[index]:
            reasons[index].append(NO_ADDRESS_REASON)
            audit.add(index, 'NO_MAILING_ADDRESS', 'address', '', 'SOURCE_DATABASE', '', '')
    result['clean_tag'] =['N' if reasons[index] else 'Y' for index in result.index]
    result['reason_for_removal'] = [
        '; '.join(reasons[index]) if reasons[index] else 'No cleanup matches found - FOR MANUAL REVIEW'
        for index in result.index
    ]
    return audit.finish(result)


def execute_po(config, ready, result_folder):
    with contextlib.closing(open_db(config['db'])) as con:
        chunks, loaded = [], 0
        for chunk in pd.read_sql_query(
            f'SELECT * FROM pooling_order WHERE {PO_WHERE} ORDER BY po_id',
            con, chunksize=25000,
        ):
            chunks.append(chunk)
            loaded += len(chunk)
            print(f'Reading PO: {loaded:,} / {ready["count"]:,} rows...')
    if not chunks:
        raise ValueError('No PO rows remain. Check references again.')
    if ready['fingerprints'] != fingerprints(config):
        raise ValueError('The database changed while reading. Check references again.')
    result = cleanup_po(pd.concat(chunks, ignore_index=True), ready)
    date = datetime.now(ZoneInfo('America/Chicago')).strftime('%Y%m%d')
    outputs = []
    for part, start in enumerate(range(0, len(result), 1048575), 1):
        suffix = f' - part {part}' if len(result) > 1048575 else ''
        target = result_folder / f'{date} - Pooling Order - With Cleanup Tagging{suffix}.xlsx'
        with pd.ExcelWriter(target, engine='xlsxwriter', engine_kwargs={
            'options': {'strings_to_formulas': False, 'strings_to_urls': False, 'constant_memory': True, 'use_zip64': True}
        }) as writer:
            write_cleanup_tabs(writer, result, result.iloc[start:start+1048575])
        outputs.append(target.name)
        print(f'Saved {target.name}')
    summary = {
        'population': 'Pooling Order', 'input_rows': loaded, 'output_rows': len(result),
        'clean_Y': int(result['clean_tag'].eq('Y').sum()),
        'excluded_N': int(result['clean_tag'].eq('N').sum()),
        'no_mailing_address_N': count_no_address(result),
        'group_duplicates_removed': 0, 'files': outputs,
        'reference_checked_at': ready['prepared_at'],
        'completed_at': datetime.now().isoformat(timespec='seconds'),
        'source_files': ready['fingerprints'], 'pipedrive_mode': config['mode'],
        'deal_filter': 366502, 'person_filter': 891899,
    }
    (result_folder / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    if config['mode'] == 'Download via API':
        ready['deals'].to_csv(result_folder / 'pipedrive_deals_366502.csv', index=False)
        ready['persons'].to_csv(result_folder / 'pipedrive_persons_891899.csv', index=False)
    print(f'COMPLETE: {len(result):,} PO rows | Y: {summary["clean_Y"]:,} | N: {summary["excluded_N"]:,} (no mailing address: {summary["no_mailing_address_N"]:,})')
    return summary


# NTM uses id for reference matching; contact_id is an internal grouping key.
NTM_POPULATION = 'NTM or Non-Targeted Marketing'
NTM_OUTPUT_COLUMNS = [
    'id', 'contact_id', 'full_name', 'first_name', 'middle_name', 'last_name',
    'contact_type', 'target_county', 'target_state', 'serial_number',
    'old_serial_number', 'input_address', 'input_city', 'input_state',
    'input_postalcode', 'md_address', 'md_city', 'md_state',
    'md_postalcode', 'phone1', 'phone2', 'phone3', 'phone4', 'phone5', 'phone6',
]
# Not used by any matching rule; carried through to the output when present,
# but a run is allowed to proceed even if the ntm table lacks them.
NTM_OPTIONAL_COLUMNS = [
    'input_postalcode', 'md_postalcode',
]
NTM_ADDRESS_SETS = (
    ('input_address', 'input_city', 'input_state'),
    ('md_address', 'md_city', 'md_state'),
)
NTM_PHONE_COLS = [f'phone{i}' for i in range(1, 7)]
NTM_SERIAL_COLS = ('serial_number', 'old_serial_number')
NTM_WHERE = (
    "UPPER(TRIM(COALESCE(contact_type, ''))) "
    "NOT IN ('COMPANY', 'RELIGIOUS INSTITUTION')"
)
NTM_READ_CHUNK = 25000
NTM_OUTPUT_CHUNK = 1000000


def ntm_id_tokens(value):
    # Numeric source IDs; split only on pipes and never extract digits from text.
    values = set()
    for token in split_pipe_values(value):
        text = report_id(token)
        if re.fullmatch(r'\d+', text):
            values.add(int(text))
    return values


def ntm_location_pair(county, state):
    # Preserve multiword counties. Never split a county on spaces or punctuation.
    return (safe_text(county).strip().upper(), safe_text(state).strip().upper())


def ntm_selected_locations(values):
    if not values:
        return set()
    if not isinstance(values, (list, tuple)):
        raise ValueError('NTM output locations must be county/state pairs.')
    selected = set()
    for pair in values:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError('Each NTM output location needs a county and a state.')
        county, state = ntm_location_pair(*pair)
        if not county or not state:
            raise ValueError('Select NTM locations with both county and state.')
        selected.add((county, state))
    return selected


def ntm_output_locations(path):
    # Read-only; run this in the UI worker, not in Tk's main thread.
    if not path or not Path(path).is_file():
        raise ValueError('Select the NTM SQLite database first.')
    with contextlib.closing(open_db(path)) as con:
        columns = [row[1] for row in con.execute('PRAGMA table_info(ntm)')]
        require_columns(columns, ['target_county', 'target_state', 'contact_type'], 'ntm table')
        values = con.execute(
            f'SELECT DISTINCT target_county, target_state FROM ntm WHERE {NTM_WHERE}'
        ).fetchall()
    options = {ntm_location_pair(county, state) for county, state in values}
    return sorted(pair for pair in options if all(pair))


def ntm_output_mask(frame, locations):
    selected = ntm_selected_locations(locations)
    if not selected:
        raise ValueError('Select at least one NTM county/state for output.')
    keys = pd.MultiIndex.from_arrays([
        frame['target_county'].map(lambda value: ntm_location_pair(value, '')[0]),
        frame['target_state'].map(lambda value: ntm_location_pair('', value)[1]),
    ])
    return keys.isin(list(selected))


def validate_ntm_db(path, selected_locations=None):
    with contextlib.closing(open_db(path)) as con:
        columns = [row[1] for row in con.execute('PRAGMA table_info(ntm)')]
        require_columns(
            columns,
            [c for c in NTM_OUTPUT_COLUMNS if c not in NTM_OPTIONAL_COLUMNS],
            'ntm table',
        )
        count, distinct_ids = con.execute(
            f'SELECT COUNT(*), COUNT(DISTINCT id) FROM ntm WHERE {NTM_WHERE}'
        ).fetchone()
    if not count:
        raise ValueError('No eligible NTM records found.')
    if distinct_ids != count:
        raise ValueError('NTM contains duplicate or NULL id values among eligible rows.')
    selected = ntm_selected_locations(selected_locations)
    if not selected:
        raise ValueError('Select at least one NTM county/state for output.')
    unavailable = selected - set(ntm_output_locations(path))
    if unavailable:
        raise ValueError('NTM output locations are no longer available: ' +
                         '; '.join(f'{county}, {state}' for county, state in sorted(unavailable)))
    print(f'NTM ready: all {count:,} eligible rows will be matched; '
          f'output is limited to {len(selected):,} county/state selections.')
    return count


def ntm_titles(row):
    names = {ncop_text(row['full_name'])}
    first, last = ncop_text(row['first_name']), ncop_text(row['last_name'])
    if first and last:
        names.add(f'{first} {last}')
    county = re.sub(r'\s+COUNTY$', '', ncop_text(row['target_county']).upper()).strip()
    state = ncop_text(row['target_state']).upper()
    if not county or not state:
        return []
    return sorted({f'{name} {county} County, {state}' for name in names if name})


def ntm_reference_lookups(ready):
    references, lookups = {}, {}
    for source, frame in (('PIPEDRIVE', ready['deals']), ('ARCHIVE', ready['archived'])):
        reference = frame.copy()
        format_pipedrive_df(reference)
        references[source] = reference
        lookups[source] = {
            'phones': get_phone_set(reference),
            'serials': get_serial_set(reference),
            'ids': {key for value in reference['Deal - Unique Database ID']
                    for key in ntm_id_tokens(value)},
            'addresses': set(reference['Person - Mailing Address'].map(normalize_address)) - {''},
        }
    return references, lookups


def cleanup_ntm_chunk(df, ready, references, lookups, evidence_indexes):
    # Chunking limits title-pattern and temporary-column memory; no dedupe here.
    result = df.copy().reset_index(drop=True)
    result['id'] = pd.to_numeric(result['id'], errors='raise').astype('Int64')
    if result['id'].isna().any() or result['id'].duplicated().any():
        raise ValueError('NTM id values must be unique nonblank integers.')
    audit = MatchEvidence(NTM_POPULATION, result, references, ready['c3'])
    audit.indexes = evidence_indexes
    reasons = {index: [] for index in result.index}

    def mark(indices, reason):
        for index in pd.Index(indices).unique():
            if reason not in reasons[index]:
                reasons[index].append(reason)

    source_labels = {'PIPEDRIVE': 'Pipedrive', 'ARCHIVE': 'Archived Pipedrive'}
    titles = result.apply(ntm_titles, axis=1).explode().dropna()
    current_mask, archive_mask = build_deal_title_masks(
        titles, references['PIPEDRIVE']['Deal - Title'], references['ARCHIVE']['Deal - Title']
    )
    audit.titles(titles, (('PIPEDRIVE', current_mask), ('ARCHIVE', archive_mask)))
    for source, mask in (('PIPEDRIVE', current_mask), ('ARCHIVE', archive_mask)):
        mark(mask[mask].index, f'Found matching Deal Title from {source_labels[source]}')

    c3_addresses, _ = ready['c3']
    for columns in NTM_ADDRESS_SETS:
        raw_addresses = result[list(columns)].apply(
            lambda row: ', '.join(ncop_text(value) for value in row if ncop_text(value)), axis=1
        )
        address_keys = raw_addresses.map(lambda value: normalize_address(remove_trailing_zip(value)))
        for source, lookup in lookups.items():
            matched = address_keys[address_keys.ne('') & address_keys.isin(lookup['addresses'])]
            mark(matched.index, f'{columns[0]}: Address found in {source_labels[source]}')
            for index, key in matched.items():
                audit.address(index, columns, key, source)
        matched = address_keys[address_keys.ne('') & address_keys.isin(c3_addresses)]
        mark(matched.index, f'{columns[0]}: Address found in C3')
        for index, key in matched.items():
            audit.address(index, columns, key, 'C3')

    for field in NTM_PHONE_COLS:
        tokens = result[field].map(ncop_phone_tokens).explode().dropna()
        for source, lookup in lookups.items():
            matched = tokens[tokens.isin(lookup['phones'])]
            mark(matched.index, f'Phone Number is in {source_labels[source]}')
            for index, key in matched.items():
                audit.equal(index, 'PHONE', field, result.at[index, field], {key},
                            source, PHONE_COLS, 'phone')
        matched = tokens[tokens.isin(audit.c3_phone_keys)]
        mark(matched.index, 'Phone Number is in C3')
        for index, key in matched.items():
            audit.c3_equal(index, 'PHONE', field, result.at[index, field], key)

    for field in NTM_SERIAL_COLS:
        # Spaces/dashes stay inside a serial number; ONLY | separates serials.
        tokens = result[field].map(split_pipe_values).explode().dropna()
        for source, lookup in lookups.items():
            matched = tokens[tokens.isin(lookup['serials'])]
            mark(matched.index, f'{field}: Serial Number is in {source_labels[source]}')
            for index, key in matched.items():
                audit.equal(index, 'SERIAL_NUMBER', field, result.at[index, field], {key},
                            source, ['Deal - Serial Number'], 'serial')

    for source, lookup in lookups.items():
        matched = result['id'][result['id'].isin(lookup['ids'])]
        mark(matched.index, f'NTM ID is in {source_labels[source]}')
        for index, key in matched.items():
            audit.equal(index, 'SOURCE_ID', 'id', key, {int(key)}, source,
                        ['Deal - Unique Database ID'], 'ntm_id')

    result['clean_tag'] = ['N' if reasons[index] else 'Y' for index in result.index]
    result['reason_for_removal'] = ['; '.join(reasons[index]) for index in result.index]
    return result, audit.rows


def finalize_ntm_groups(frame, events, selected_locations):
    # Every eligible row has already been matched, including every chunk.
    frame = frame.copy()
    frame['_ntm_group_key'] = frame['contact_id'].map(report_id)
    direct_match = frame['clean_tag'].eq('N')
    grouped = frame['_ntm_group_key'].ne('')
    group_has_match = direct_match.groupby(frame['_ntm_group_key'], sort=False).transform('any')
    frame['clean_tag'] = np.where(direct_match | (grouped & group_has_match), 'N', 'Y')

    def combine_reasons(values):
        return '; '.join(dict.fromkeys(
            part for value in values for part in str(value).split('; ') if part
        ))

    group_reasons = frame.loc[grouped & direct_match].groupby(
        '_ntm_group_key', sort=False
    )['reason_for_removal'].agg(combine_reasons)
    inherited = grouped & ~direct_match & group_has_match
    group_n = grouped & frame['clean_tag'].eq('N')
    frame.loc[group_n, 'reason_for_removal'] = frame.loc[group_n, '_ntm_group_key'].map(group_reasons)
    frame.loc[inherited, 'reason_for_removal'] = (
        frame.loc[inherited, 'reason_for_removal']
        + '; Group exclusion: another record with the same contact_id matched'
    )
    frame.loc[frame['clean_tag'].eq('Y'), 'reason_for_removal'] = (
        'No cleanup matches found - FOR MANUAL REVIEW'
    )
    # Propagation above used ALL eligible counties, including unselected counties.
    # Select output rows BEFORE deduplication, so an unselected lower ID cannot
    # replace the selected county's row. Keep one record per selected contact.
    frame = frame.loc[ntm_output_mask(frame, selected_locations)].copy()
    selected_before_dedup = len(frame)
    if not selected_before_dedup:
        raise ValueError('No eligible NTM rows belong to the selected output locations.')
    # Retain a row with a valid mailing address first, then the lowest numeric id,
    # among SELECTED rows; blank groups stay separate.
    frame['_has_addr'] = has_mailing_address(frame, NTM_ADDRESS_SETS)
    frame = frame.sort_values(['_has_addr', 'id'], ascending=[False, True], kind='stable')
    keep = frame['_ntm_group_key'].eq('') | ~frame['_ntm_group_key'].duplicated()
    result = frame.loc[keep].sort_values('id', kind='stable').copy()
    no_address = result['clean_tag'].eq('Y') & ~result['_has_addr']
    result.loc[no_address, 'clean_tag'] = 'N'
    result.loc[no_address, 'reason_for_removal'] = NO_ADDRESS_REASON
    events = list(events) + [
        {
            'population': NTM_POPULATION, 'cleanup_record_id': '',
            'evidence_record_id': report_id(row['id']),
            'contact_group_id': '', 'group_key': row['_ntm_group_key'],
            'match_type': 'NO_MAILING_ADDRESS', 'source_field': 'address',
            'source_value': '', 'matched_key': '',
            'reference_source': 'SOURCE_DATABASE', 'reference_field': '',
            'reference_value': '', 'reference_value_format': 'RAW',
            'reference_id': '',
        }
        for _, row in result.loc[no_address].iterrows()
    ]
    print(f'NTM: {int(no_address.sum()):,} clean rows tagged N for no valid mailing address.')
    retained_groups = dict(zip(
        result.loc[result['_ntm_group_key'].ne(''), '_ntm_group_key'],
        result.loc[result['_ntm_group_key'].ne(''), 'id'].map(report_id),
    ))
    excluded_ids = set(result.loc[result['clean_tag'].eq('N'), 'id'].map(report_id))
    details = []
    evidence_for = set()
    for event in events:
        group_key = event.get('group_key', '')
        target = retained_groups.get(group_key, event['evidence_record_id'])
        if target in excluded_ids:
            details.append(dict(event, cleanup_record_id=target))
            evidence_for.add(target)
    if evidence_for != excluded_ids:
        raise ValueError('An excluded NTM output row has no matching evidence.')
    result = result[NTM_OUTPUT_COLUMNS + ['clean_tag', 'reason_for_removal']].reset_index(drop=True)
    for column in ['id', 'contact_id', 'input_postalcode', 'md_postalcode']:
        result[column] = result[column].map(report_id)
    for column in NTM_PHONE_COLS:
        result[column] = result[column].map(report_phone_cell)
    result.attrs['match_details'] = details
    result.attrs['match_id_column'] = 'id'
    result.attrs['match_population'] = NTM_POPULATION
    result.attrs['match_group_column'] = 'contact_id'
    result.attrs['ntm_selected_before_dedup'] = selected_before_dedup
    return result


def write_ntm_tabs(writer, detail_context, subset):
    # Direct, row-by-row writing is compatible with XlsxWriter constant_memory.
    # Do not turn constant_memory on for the existing pandas to_excel writer.
    print(f'Building Match Details for {len(subset):,} selected Cleanup rows...')
    details = wide_match_details(detail_context, subset)
    date_format = writer.book.add_format({'num_format': 'yyyy-mm-dd hh:mm:ss'})
    for name, data in (('Cleanup', subset), ('Match Details', details)):
        if len(data) > 1048575 or len(data.columns) > 16384:
            raise ValueError(f'{name} exceeds the Excel row or column limit.')
        sheet = writer.book.add_worksheet(name)
        writer.sheets[name] = sheet
        sheet.write_row(0, 0, list(data.columns))
        print(f'Writing {name}: {len(data):,} rows...')
        for number, values in enumerate(data.itertuples(index=False, name=None), 1):
            cells = []
            for value in values:
                if pd.isna(value):
                    value = None
                elif isinstance(value, np.generic):
                    value = value.item()
                if isinstance(value, str) and len(value) > 32767:
                    raise ValueError(f'Excel cell limit exceeded in {name}, row {number+1}.')
                cells.append(value)
            error = sheet.write_row(number, 0, cells, date_format)
            if error:
                raise ValueError(f'Excel write failed in {name}, row {number+1}: {error}')
            if number % 10000 == 0:
                print(f'{name}: wrote {number:,} / {len(data):,} rows...')
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, len(data), len(data.columns)-1)
        if name == 'Match Details':
            sheet.set_column(0, 0, 18)
            sheet.set_column(1, len(data.columns)-1, 30)
        print(f'{name}: finished {len(data):,} rows.')


def execute_ntm(config, ready, result_folder):
    references, lookups = ntm_reference_lookups(ready)
    evidence_indexes = {}
    chunks, events, loaded = [], [], 0
    with contextlib.closing(open_db(config['db'])) as con:
        table_columns = {row[1] for row in con.execute('PRAGMA table_info(ntm)')}
        available_columns = [c for c in NTM_OUTPUT_COLUMNS if c in table_columns]
        columns = ', '.join('"' + column + '"' for column in available_columns)
        for chunk in pd.read_sql_query(
            f'SELECT {columns} FROM ntm WHERE {NTM_WHERE} ORDER BY id',
            con, chunksize=NTM_READ_CHUNK,
        ):
            for column in NTM_OUTPUT_COLUMNS:
                if column not in chunk.columns:
                    chunk[column] = pd.NA
            cleaned, chunk_events = cleanup_ntm_chunk(
                chunk, ready, references, lookups, evidence_indexes
            )
            chunks.append(cleaned)
            events.extend(chunk_events)
            loaded += len(chunk)
            print(f'NTM checked: {loaded:,} / {ready["count"]:,} source rows...')
    if not chunks:
        raise ValueError('No NTM rows remain. Check references again.')
    if ready['fingerprints'] != fingerprints(config):
        raise ValueError('The database changed during matching. Check references again.')
    frame = pd.concat(chunks, ignore_index=True)
    chunks.clear()
    if frame['id'].duplicated().any():
        raise ValueError('NTM contains duplicate id values across input chunks.')
    print('Matching complete. Applying contact-group exclusions and output selections...')
    selected_locations = sorted(ntm_selected_locations(config.get('ntm_output_locations')))
    result = finalize_ntm_groups(frame, events, selected_locations)
    # Keep large evidence metadata OFF result while slicing it for Excel.
    # Pandas can copy DataFrame.attrs during indexing; evidence is not row data.
    detail_context = pd.DataFrame()
    detail_context.attrs = dict(result.attrs)
    selected_before_dedup = result.attrs['ntm_selected_before_dedup']
    result.attrs = {}
    print(f'Exporting {len(result):,} rows from {len(selected_locations):,} selections '
          f'after matching all {loaded:,} eligible rows.')
    del frame, events, evidence_indexes, references, lookups
    date = datetime.now(ZoneInfo('America/Chicago')).strftime('%Y%m%d')
    outputs = []
    for part, start in enumerate(range(0, len(result), NTM_OUTPUT_CHUNK), 1):
        suffix = f' - part {part}' if len(result) >= NTM_OUTPUT_CHUNK else ''
        target = result_folder / f'{date} - NTM - With Cleanup Tagging{suffix}.xlsx'
        print(f'Writing NTM Excel file {part}...')
        with pd.ExcelWriter(target, engine='xlsxwriter', engine_kwargs={
            'options': {'strings_to_formulas': False, 'strings_to_urls': False,
                        'constant_memory': True}
        }) as writer:
            write_ntm_tabs(writer, detail_context, result.iloc[start:start+NTM_OUTPUT_CHUNK])
            print(f'Finalizing NTM Excel file {part}; please wait...')
        outputs.append(target.name)
        print(f'Saved {target.name}')
    summary = {
        'population': NTM_POPULATION, 'input_rows': loaded, 'output_rows': len(result),
        'clean_Y': int(result['clean_tag'].eq('Y').sum()),
        'excluded_N': int(result['clean_tag'].eq('N').sum()),
        'no_mailing_address_N': count_no_address(result),
        'selected_rows_before_dedup': selected_before_dedup,
        'output_locations': [list(pair) for pair in selected_locations],
        'group_duplicates_removed': selected_before_dedup - len(result), 'files': outputs,
        'reference_checked_at': ready['prepared_at'],
        'completed_at': datetime.now().isoformat(timespec='seconds'),
        'source_files': ready['fingerprints'], 'pipedrive_mode': config['mode'],
        'deal_filter': 366502, 'person_filter': 891899,
    }
    (result_folder / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    if config['mode'] == 'Download via API':
        ready['deals'].to_csv(result_folder / 'pipedrive_deals_366502.csv', index=False)
        ready['persons'].to_csv(result_folder / 'pipedrive_persons_891899.csv', index=False)
    print(f'COMPLETE: {len(result):,} NTM rows | Y: {summary["clean_Y"]:,} | N: {summary["excluded_N"]:,} (no mailing address: {summary["no_mailing_address_N"]:,})')
    return summary


# Pipedrive-based populations: source deals are checked against OTHER deal IDs.
CAMPAIGN_FILTERS = {
    'Conversion Campaign: Prio 2': 365288,
    'Conversion Campaign: Prio 3': 365289,
    'Conversion Campaign: Prio 4': 365290,
    'JR AA Sales Recycled': 393096,
    'Mid AA Sales Recycled': 393116,
    'SR AA Sales Recycled': 393119,
}
EMAIL_COLS = ['Person - Email - ' + label for label in ('Home', 'Other', 'Work')]
CAMPAIGN_COLS = [
    'Deal - ID', 'Deal - Pipeline', 'Deal - Stage', 'Deal - Owner',
    'Deal - Title', 'Deal - Value', 'Deal - Deal created', 'Deal - Deal Status',
    'Deal - Deal Summary', 'Deal - Unique Database ID', 'Deal - BU Database ID',
    'Deal - Contact Group ID', 'Deal - Contact person', 'Deal - County',
    'Deal - County (Old)', 'Deal - Last RVM Date', 'Deal - Marketing Medium',
    'Person - Mailing Address', *EMAIL_COLS,
    'Person - Phone - Home', 'Person - Phone - Mobile',
    'Person - Phone - Other', 'Person - Phone - Work',
    # Required by the matching rules, in addition to the requested display fields.
    'Deal - NCOP ID', 'Deal - Pooling Order ID', 'Deal - Serial Number',
]
CAMPAIGN_ID_CHECKS = (
    ('Deal - BU Database ID', 'BU_DATABASE_ID', 'bu_database_id', 'integer'),
    ('Deal - NCOP ID', 'NCOP_ID', 'ncop_id', 'integer'),
    ('Deal - Unique Database ID', 'UNIQUE_DATABASE_ID', 'unique_database_id', 'ntm_id'),
    ('Deal - Pooling Order ID', 'POOLING_ORDER_ID', 'pooling_order_id', 'po_id'),
    ('Deal - Contact Group ID', 'CONTACT_GROUP_ID', 'contact_group_id', 'integer'),
    ('Deal - Serial Number', 'SERIAL_NUMBER', 'serial_number', 'serial'),
)
CAMPAIGN_EXTRA_MATCH_TYPES = (
    ('EMAIL', 'email'), ('BU_DATABASE_ID', 'bu_database_id'),
    ('NCOP_ID', 'ncop_id'), ('UNIQUE_DATABASE_ID', 'unique_database_id'),
    ('POOLING_ORDER_ID', 'pooling_order_id'),
)


def is_campaign_population(population):
    return population in CAMPAIGN_FILTERS


def campaign_filter_url(population):
    return (f'https://{PIPEDRIVE_DOMAIN}/deals/filter/'
            f'{CAMPAIGN_FILTERS[population]}?quickFilter=none')

def campaign_email_tokens(value):
    # Retain punctuation: a.b@example.com must not become ab@example.com.
    return {part.strip().casefold() for part in match_text(value).split(',') if part.strip()}


def campaign_tokens(value, mode):
    if mode == 'phone':
        return {str(number) for number in match_phone_keys(value)}
    if mode == 'email':
        return campaign_email_tokens(value)
    if mode == 'address':
        return {normalize_address(remove_trailing_zip(value))} - {''}
    if mode == 'integer':
        return {str(number) for part in split_pipe_values(value)
                if (number := extract_int_token(part)) is not None}
    if mode == 'ntm_id':
        return {str(number) for number in ntm_id_tokens(value)}
    if mode == 'po_id':
        return po_id_tokens(value)
    if mode == 'serial':
        return {normalize_serial_token(part) for part in split_pipe_values(value)} - {''}
    raise ValueError(f'Unknown matching mode: {mode}')


def validate_campaign_db(path, _selected_filter=None):
    # The real source can be a local export or an API filter, not a SQLite DB.
    raise ValueError('Use the campaign population loader in prepare().')


def validate_campaign_frame(frame, label='Population export'):
    require_columns(frame.columns, CAMPAIGN_COLS, label)
    frame = with_reference_ids(frame, 'Deal - ID', (), label)
    if frame.empty:
        raise ValueError(f'{label} is empty.')
    if frame['Deal - ID'].duplicated().any():
        raise ValueError(f'{label} contains duplicate Deal - ID rows; select one row per deal.')
    return frame


def campaign_custom_value(item, key):
    fields = item.get('custom_fields') or {}
    if key not in fields and key not in item:
        raise ValueError('A required population custom field was omitted; use a complete export.')
    def display(value):
        if value is None:
            return ''
        if isinstance(value, list):
            return ' | '.join(display(part) for part in value)
        if isinstance(value, dict):
            for field in ('label', 'value'):
                if field in value:
                    return display(value[field])
            raise ValueError('Unsupported population custom field structure; use a local export.')
        return str(value)
    return display(fields.get(key, item.get(key)))


def download_campaign_population(token, population, persons):
    require_columns(
        persons.columns, PERSON_COLS + EMAIL_COLS + ['Person - Name'],
        'Persons reference for API population',
    )
    api = Pipedrive(token)
    users_api = Pipedrive(token)
    users_api.ROOT = api.ROOT.replace('/v2/', '/v1/')
    try:
        names = [field.removeprefix('Deal - ') for field, *_ in CAMPAIGN_ID_CHECKS]
        names += ['Deal Summary', 'County', 'County (Old)', 'Last RVM Date', 'Marketing Medium']
        keys = api.fields('dealFields', names)
        address_key = api.fields('personFields', ['Mailing Address'])['Mailing Address']
        deals = api.all('deals', filter_id=CAMPAIGN_FILTERS[population],
                        custom_fields=','.join(keys.values()), include_option_labels='true')
        if not deals:
            raise ValueError('The selected population filter returned no deals.')
        pipelines = {report_id(item['id']): item['name'] for item in api.all('pipelines')}
        stages = {report_id(item['id']): item['name'] for item in api.all('stages')}
        users = {report_id(item['id']): item['name']
                 for item in (users_api.get('users', {}).get('data') or [])}
        # Use the already-loaded, export-shaped person records.
        person_ids = pd.to_numeric(
            persons['Person - ID'], errors='raise'
        ).astype('Int64')

        if person_ids.isna().any() or person_ids.duplicated().any():
            raise ValueError(
                'Persons reference has blank or duplicate Person IDs.'
            )

        wanted = {
            int(deal['person_id'])
            for deal in deals
            if deal.get('person_id') is not None
        }

        selected = persons.loc[person_ids.isin(wanted)]

        people = {
            int(report_id(person['Person - ID'])): person
            for person in selected.to_dict('records')
        }

        missing = sorted(wanted - set(people))

        print(
            f'Population contacts: reusing {len(people):,} / '
            f'{len(wanted):,} unique linked persons '
            'from the loaded reference.'
        )

        if not missing:
            print(
                'All linked persons are already available. '
                'No additional person download needed.'
            )

        for start in range(0, len(missing), 100):
            batch = missing[start:start+100]

            print(
                'Fetching missing population contacts: '
                f'{start+1:,}-{start+len(batch):,} / '
                f'{len(missing):,}...'
            )

            records = api.all(
                'persons',
                ids=','.join(map(str, batch)),
                custom_fields=address_key,
            )

            people.update({
                int(person['id']): person_row(person, address_key)
                for person in records
            })

        if wanted - set(people):
            raise ValueError(
                'Some population contacts are inaccessible. '
                'Use a complete local export.'
            )

        def label(lookup, value, field):
            key = report_id(value)
            if not key:
                return ''
            if key not in lookup:
                raise ValueError(f'Cannot resolve population {field}; use a local export.')
            return lookup[key]

        rows = []
        for deal in deals:
            person = people.get(int(deal['person_id'])) if deal.get('person_id') is not None else None
            row = dict.fromkeys(CAMPAIGN_COLS, '')
            row.update({
                'Deal - ID': report_id(deal['id']),
                'Deal - Pipeline': label(pipelines, deal.get('pipeline_id'), 'pipeline'),
                'Deal - Stage': label(stages, deal.get('stage_id'), 'stage'),
                'Deal - Owner': label(users, deal.get('owner_id'), 'owner'),
                'Deal - Title': deal.get('title'), 'Deal - Value': deal.get('value'),
                'Deal - Deal created': deal.get('add_time'),
                'Deal - Deal Status': deal.get('status'),
                'Deal - Contact person': person.get('Person - Name', '') if person else '',
            })
            row.update({'Deal - ' + name: campaign_custom_value(deal, key) for name, key in keys.items()})
            if person:
                row.update(person)
            rows.append(row)
        return validate_campaign_frame(pd.DataFrame(rows, columns=CAMPAIGN_COLS), 'API population')
    finally:
        api.token = users_api.token = ''


def load_campaign_population(config, persons):
    if config.get('population_mode') == 'Manual files':
        return validate_campaign_frame(read_table(config['population_file'], CAMPAIGN_COLS, 'Population export'))
    if config.get('population_mode') != 'Download via API':
        raise ValueError('Choose API or a local export for the population.')
    import hashlib
    import tempfile
    token = config.get('token', '').strip()
    if not token:
        raise ValueError('Enter your shared Pipedrive API token.')
    scope = f"{Pipedrive.ROOT}|{CAMPAIGN_FILTERS[config['population']]}|campaign-v1|{token}"
    folder = BASE / 'pipedrive_cache' / hashlib.sha256(scope.encode()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    target, manifest = folder / 'population.csv', folder / 'metadata.json'
    try:
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        stamp = float(metadata['downloaded_at'])
        if not 0 <= time.time()-stamp < 8*60*60:
            raise ValueError('Population cache expired.')
        with target.open('rb') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != metadata['sha256']:
                raise ValueError('Population cache changed.')
            handle.seek(0)
            frame = validate_campaign_frame(pd.read_csv(handle, dtype=str, keep_default_na=False))
        if len(frame) != metadata['rows'] or not 0 <= time.time()-stamp < 8*60*60:
            raise ValueError('Population cache is incomplete or expired.')
        print('Using saved population download under 8 hours old.')
    except (OSError, ValueError, KeyError, TypeError):
        print(f"Downloading population filter {CAMPAIGN_FILTERS[config['population']]}...")
        frame = download_campaign_population(token, config['population'], persons)
        stamp = time.time()
        with tempfile.TemporaryDirectory(dir=folder) as temporary:
            temporary = Path(temporary)
            path = temporary / 'population.csv'
            frame.to_csv(path, index=False, encoding='utf-8-sig')
            with path.open('rb') as handle:
                checksum = hashlib.file_digest(handle, 'sha256').hexdigest()
            (temporary / 'metadata.json').write_text(json.dumps({
                'downloaded_at': stamp, 'sha256': checksum, 'rows': len(frame),
            }), encoding='utf-8')
            os.replace(path, target)
            os.replace(temporary / 'metadata.json', manifest)
    frame.attrs['population_downloaded_at'] = stamp
    return frame


def cleanup_campaign(frame, ready, population):
    result = validate_campaign_frame(frame).copy()
    # Original source columns stay intact. Keys live only in lookup structures.
    reasons = {index: [] for index in result.index}
    events, seen = [], set()
    sources = {kind: {source: set() for source in ('PIPEDRIVE', 'ARCHIVE', 'C3')}
               for kind in ('ADDRESS', 'PHONE', 'EMAIL', 'SERIAL_NUMBER', 'SOURCE_ID', 'CONTACT_GROUP_ID')}
    checks = [
        ('ADDRESS', ('Person - Mailing Address',), 'address', True),
        ('PHONE', tuple(PHONE_COLS), 'phone', True),
        ('EMAIL', tuple(EMAIL_COLS), 'email', True),
    ] + [(kind, (field,), mode, False) for field, kind, _, mode in CAMPAIGN_ID_CHECKS]
    for kind, fields, mode, use_c3 in checks:
        summary_kind = 'SOURCE_ID' if kind in ('BU_DATABASE_ID', 'NCOP_ID', 'UNIQUE_DATABASE_ID', 'POOLING_ORDER_ID') else kind
        lookups = {}
        for source, reference in (('PIPEDRIVE', ready['deals']), ('ARCHIVE', ready['archived'])):
            lookup = {}
            for field in fields:
                for value, deal_id in reference[[field, 'Deal - ID']].itertuples(index=False, name=None):
                    for key in campaign_tokens(value, mode):
                        lookup.setdefault(key, set()).add(report_id(deal_id))
            lookups[source] = lookup
        if use_c3:
            map_kind = 'PHONE_NUMERIC' if kind == 'PHONE' else kind
            lookups['C3'] = ready['c3'].id_maps[map_kind]
        for index, row in result.iterrows():
            current_id = report_id(row['Deal - ID'])
            for field in fields:
                for key in campaign_tokens(row[field], mode):
                    for source, lookup in lookups.items():
                        targets = lookup.get(key, set())
                        if source != 'C3':
                            targets = targets - {current_id}
                        if not targets:
                            continue
                        reason = f'{source}: {field}'
                        if reason not in reasons[index]:
                            reasons[index].append(reason)
                        sources[summary_kind][source].add(index)
                        for target_id in sorted(targets):
                            signature = (current_id, kind, key, source, report_id(target_id))
                            if signature in seen:
                                continue
                            seen.add(signature)
                            events.append({
                                'cleanup_record_id': current_id, 'evidence_record_id': current_id,
                                'match_type': kind, 'reference_source': source,
                                'matched_key': key,
                                'source_value': match_text(row[field]) if kind == 'ADDRESS' else key,
                                'reference_id': report_id(target_id),
                            })
        print(f'Campaign check complete: {kind} ({len(result):,} rows).')
    # A single free-text field: any non-blank address counts as mailable.
    has_address = result['Person - Mailing Address'].map(
        lambda value: bool(campaign_tokens(address_part(value), 'address'))
    )
    for index in result.index:
        if not reasons[index] and not has_address.at[index]:
            reasons[index].append(NO_ADDRESS_REASON)
    result['clean_tag'] =['N' if reasons[index] else 'Y' for index in result.index]
    result['reason_for_removal'] = ['; '.join(reasons[index]) if reasons[index]
                                  else 'No cleanup matches found - FOR MANUAL REVIEW' for index in result.index]
    result['matching_status'] = 'CHECKED'
    result['existing_deal_id'] = 'N/A'
    result['match_deal_title'] = 'N/A'
    for kind, column in (('ADDRESS', 'match_address'), ('PHONE', 'match_phone'), ('EMAIL', 'match_email'),
                         ('SERIAL_NUMBER', 'match_serial_number'), ('SOURCE_ID', 'match_source_id'),
                         ('CONTACT_GROUP_ID', 'match_contact_group_id')):
        if kind == 'SOURCE_ID':
            present = result[[field for field, _, _, _ in CAMPAIGN_ID_CHECKS[:4]]].apply(
                lambda row: any(campaign_tokens(row[field], mode) for field, _, _, mode in CAMPAIGN_ID_CHECKS[:4]), axis=1)
        else:
            _, fields, mode, _ = next(check for check in checks if check[0] == kind)
            present = result[list(fields)].apply(lambda row: any(campaign_tokens(value, mode) for value in row), axis=1)
        result[column] = [
            ' | '.join(source for source in ('PIPEDRIVE', 'ARCHIVE', 'C3') if index in sources[kind][source])
            or ('NO_MATCH' if present.loc[index] else 'NO_VALUE') for index in result.index
        ]
    result['excluded_via_contact_group'] = 'N/A'
    for column in PHONE_COLS:
        result[column] = result[column].map(report_phone_cell)
    for field, _, _, mode in CAMPAIGN_ID_CHECKS:
        if mode != 'serial':
            result[field] = result[field].map(
                lambda value: ' | '.join(report_id(part) for part in split_pipe_values(value))
            )
    result.attrs = {'match_details': events, 'match_id_column': 'Deal - ID',
                    'match_population': population, 'match_group_column': ''}
    return result


def execute_campaign(config, ready, result_folder):
    if config.get('population_mode') == 'Download via API':
        stamp = ready['population_frame'].attrs.get('population_downloaded_at')
        if stamp is None or not 0 <= time.time()-stamp < 8*60*60:
            raise ValueError('Population download expired. Check references again.')
    result = cleanup_campaign(ready['population_frame'], ready, config['population'])
    context = pd.DataFrame()
    context.attrs = dict(result.attrs)
    result.attrs = {}
    date = datetime.now(ZoneInfo('America/Chicago')).strftime('%Y%m%d')
    name = re.sub(r'[<>:"/\\|?*]', '', config['population'])
    outputs = []
    for part, start in enumerate(range(0, len(result), 1000000), 1):
        suffix = f' - part {part}' if len(result) >= 1000000 else ''
        target = result_folder / f'{date} - {name} - With Cleanup Tagging{suffix}.xlsx'
        with pd.ExcelWriter(target, engine='xlsxwriter', engine_kwargs={'options': {
            'constant_memory': True, 'strings_to_formulas': False, 'strings_to_urls': False,
        }}) as writer:
            write_ntm_tabs(writer, context, result.iloc[start:start+1000000])
            print(f'Finalizing {target.name}...')
        outputs.append(target.name)
    summary = {'population': config['population'], 'input_rows': len(result), 'output_rows': len(result),
               'clean_Y': int(result['clean_tag'].eq('Y').sum()),
               'excluded_N': int(result['clean_tag'].eq('N').sum()),
               'no_mailing_address_N': count_no_address(result), 'files': outputs,
               'population_mode': config['population_mode'],
               'population_filter': CAMPAIGN_FILTERS[config['population']],
               'reference_checked_at': ready['prepared_at'], 'source_files': ready['fingerprints'],
               'pipedrive_mode': config['mode'], 'completed_at': datetime.now().isoformat(timespec='seconds')}
    (result_folder / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(f'COMPLETE: {len(result):,} campaign rows | Y: {summary["clean_Y"]:,} | N: {summary["excluded_N"]:,} (no mailing address: {summary["no_mailing_address_N"]:,})')
    return summary


def prepare(config):
    validate_pipedrive_mode(config)
    settings = require_population(config['population'])
    campaign = is_campaign_population(config['population'])
    before = fingerprints(config)
    selected_filter = (
        config.get('ncop_scope') if config['population'] == 'NCOP' else
        config.get('ntm_output_locations') if config['population'] == NTM_POPULATION else
        config.get('category')
    )
    count = None if campaign else globals()[settings['validate']](config['db'], selected_filter)
    output = Path(config['output'])
    output.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.TemporaryFile(dir=output):
        pass

    deal_required, archive_required = reference_columns(config['population'])
    c3 = load_c3(config['c3'])
    archive = read_table(config['archive'], archive_required, 'Archived deals')

    if archive.empty:
        raise ValueError('The archived deals file is empty. Select the latest full archive.')
    if config['mode'] == 'Download via API':
        if not config['token'].strip():
            raise ValueError('Enter a Pipedrive API token, or select Manual files.')
        deals, persons = load_or_download_pipedrive(
            config['token'].strip()
        )
    else:
        deals = read_table(config['deals'], deal_required, 'Pipedrive deals')
        persons_required = PERSON_COLS + EMAIL_COLS if campaign else PERSON_COLS
        persons = read_table(config['persons'], persons_required, 'Pipedrive persons')
        if deals.empty or persons.empty:
            raise ValueError('Pipedrive exports must not be empty.')
    deals = with_reference_ids(deals, 'Deal - ID', ('Deal ID', 'ID'), 'Pipedrive deals')
    if campaign:
        require_columns(deals.columns, deal_required, 'Pipedrive deals')
    archived = join_archive(archive, persons)
    population_frame = (load_campaign_population(config, persons) if campaign else None)
    if campaign:
        count = len(population_frame)
        print(f'Population ready: {count:,} deals; own Deal IDs will be ignored during reference matching.')
    if before != fingerprints(config):
        raise ValueError('An input changed during checking. Check the files again.')
    return {'deals': deals, 'persons': persons, 'archived': archived, 'c3': c3,
            'selection': selection_signature(config), 'fingerprints': before, 'count': count, 'prepared_at': datetime.now().isoformat(timespec='seconds'),
            'population_frame': population_frame}


def execute_cleanup(config, ready, result_folder):
    validate_pipedrive_mode(config)
    settings = require_population(config['population'])
    if ready.get('selection') != selection_signature(config):
        raise ValueError('Population or selection changed. Check references again.')
    if ready['fingerprints'] != fingerprints(config):
        raise ValueError('An input file changed. Check references again.')
    if config['mode'] == 'Download via API':
        downloaded_at = ready['deals'].attrs.get('pipedrive_downloaded_at')
        if downloaded_at is None or not 0 <= time.time() - downloaded_at < 8*60*60:
            raise ValueError('Pipedrive downloads expired. Check references again.')
    return globals()[settings['execute']](config, ready, Path(result_folder))



def execute_bottoms_up(config, ready, result_folder):
    if config['mode'] == 'Download via API':
        downloaded_at = ready['deals'].attrs.get(
            'pipedrive_downloaded_at'
        )

        if (
            downloaded_at is None
            or not 0 <= time.time() - downloaded_at < 8 * 60 * 60
        ):
            raise ValueError(
                'The Pipedrive downloads are now 8 hours old '
                'or their download time is unavailable. '
                'Click CHECK FILES / DOWNLOAD again to refresh them.'
            )

    if ready['fingerprints'] != fingerprints(config):
        raise ValueError('An input file changed. Check files again before running.')
    with contextlib.closing(open_db(config['db'])) as con:
        chunks, loaded = [], 0
        for chunk in pd.read_sql_query('SELECT * FROM bottoms_up WHERE Category = ? ORDER BY id', con,
                                      params=(config['category'],), chunksize=25000):
            chunks.append(chunk)
            loaded += len(chunk)
            print(f'Reading {config["category"]}: {loaded:,} / {ready["count"]:,} records...')
        if not chunks:
            raise ValueError('No category records remain. Check inputs again.')
        df = pd.concat(chunks, ignore_index=True)
    if ready['fingerprints'] != fingerprints(config):
        raise ValueError('The database changed while reading. Check inputs again.')
    # SQL permits numeric values in text-affinity columns. Validate rather than silently discard them.
    df['id'] = pd.to_numeric(df['id'], errors='raise').astype('Int64')
    for col in ['Input: Address', 'Input: City', 'Input: State', 'md_address', 'md_city', 'md_state']:
        df[col] = df[col].where(df[col].notna(), '')
    result = cleanup(df, ready['deals'].copy(), ready['archived'].copy(), ready['c3'])
    date = datetime.now(ZoneInfo('America/Chicago')).strftime('%Y%m%d')
    name = f'{date} - (With Cleanup Tagging) Consolidated {config["category"]} List'
    outputs = []
    for part, start in enumerate(range(0, len(result), 1048575), 1):
        suffix = f' - part {part}' if len(result) > 1048575 else ''
        target = result_folder / (name + suffix + '.xlsx')
        print(f'Writing Excel file {part}. This can take a few minutes...')
        file_start = time.perf_counter()
        # Explicit strings_to_formulas=False preserves source text in spreadsheet output.
        # constant_memory is safe here because write_cleanup_tabs writes rows directly
        # (sheet.write_row) instead of pandas' bulk to_excel, which constant_memory forbids.
        with pd.ExcelWriter(target, engine='xlsxwriter', engine_kwargs={'options': {'strings_to_formulas': False, 'strings_to_urls': False, 'constant_memory': True, 'use_zip64': True}}) as writer:
            write_cleanup_tabs(writer, result, result.iloc[start:start+1048575])
        outputs.append(target.name)
        print(f'Saved {target.name} in {time.perf_counter()-file_start:.1f}s')
    summary = {'population': config['population'], 'category': config['category'], 'input_rows': loaded,
               'output_rows': len(result), 'clean_Y': int(result['clean_tag'].eq('Y').sum()),
               'excluded_N': int(result['clean_tag'].eq('N').sum()),
               'no_mailing_address_N': count_no_address(result), 'group_duplicates_removed': loaded - len(result),
               'reference_checked_at': ready['prepared_at'], 'completed_at': datetime.now().isoformat(timespec='seconds'),
               'source_files': ready['fingerprints'], 'pipedrive_mode': config['mode'],
               'deal_filter': 366502, 'person_filter': 891899, 'files': outputs}
    (result_folder / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    if config['mode'] == 'Download via API':
        print('Saving the downloaded Pipedrive reference snapshots...')
        ready['deals'].to_csv(result_folder / 'pipedrive_deals_366502.csv', index=False)
        ready['persons'].to_csv(result_folder / 'pipedrive_persons_891899.csv', index=False)
    print(f'COMPLETE: {len(result):,} output rows | Y: {summary["clean_Y"]:,} | N: {summary["excluded_N"]:,} (no mailing address: {summary["no_mailing_address_N"]:,})')
    return summary


def run_cleanup(config, ready, on_folder=None):
    """Create a run folder and mark partial output until execution succeeds.

    on_folder is an optional callback receiving its Path (for progress display).
    All processing/output lifecycle remains independent of the UI.
    """
    require_population(config['population'])
    result_folder = Path(config['output']) / datetime.now().strftime('run_%Y%m%d_%H%M%S_%f')
    result_folder.mkdir(parents=True, exist_ok=False)
    (result_folder / 'INCOMPLETE.txt').write_text(
        'This run has not completed. Do not use partial output.\n', encoding='utf-8')
    if on_folder is not None:
        on_folder(result_folder)
    summary = execute_cleanup(config, ready, result_folder)
    (result_folder / 'INCOMPLETE.txt').unlink()
    return summary
