# -------------------------ABOUT --------------------------

# Tool: Mail Marketing Cleanup Tool
# Developer: dyoliya
# Created: 2026-10-27

# © 2026 dyoliya. All rights reserved.

# ---------------------------------------------------------

import os
import sys
import threading
import queue
import time
import traceback
import webbrowser
import subprocess
import contextlib
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import font as tkfont
import customtkinter as ctk
from PIL import Image, ImageDraw

import mail_marketing_cleanup as backend

BASE = backend.BASE
CATEGORIES = backend.CATEGORIES
POPULATIONS = backend.POPULATIONS
DEALS_URL = backend.DEALS_URL
PERSONS_URL = backend.PERSONS_URL
ARCHIVE_URL = backend.ARCHIVE_URL
API_TOKEN_URL = f'https://{backend.PIPEDRIVE_DOMAIN}/settings/api'
ALLOW_MANUAL_PIPEDRIVE = backend.ALLOW_MANUAL_PIPEDRIVE


class ActivityWriter:
    def __init__(self, events):
        self.events = events
        self.pending = ''

    def write(self, text):
        self.pending += text
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            if line.strip():
                self.events.put(('log', line))
        return len(text)

    def flush(self):
        if self.pending.strip():
            self.events.put(('log', self.pending))
            self.pending = ''


def open_folder(path):
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'win32':
        os.startfile(str(path))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)])


class CleanupApp(ctk.CTk):
    CREAM, NAVY, ORANGE, RED = '#fff6de', '#273946', '#ffab4c', '#cb1f47'
    GREEN, MUTED, CARD, LINE = '#26734b', '#6b7880', '#fffdf7', '#e8e2d4'

    def __init__(self):
        ctk.set_appearance_mode('light')
        super().__init__()
        families = {name.lower(): name for name in tkfont.families(self)}
        self.UI_FONT = next((families[name] for name in ('segoe ui', 'inter', 'dejavu sans', 'helvetica') if name in families), 'Helvetica')
        self.LOG_FONT = next((families[name] for name in ('consolas', 'dejavu sans mono', 'courier') if name in families), 'Courier')
        self.title('Mail Marketing Cleanup v1.0.0')
        # Convert screen pixels to CustomTkinter's logical window dimensions.
        scaling = self._get_window_scaling()
        width = min(1240, int((self.winfo_screenwidth() - 70) / scaling))
        height = min(820, int((self.winfo_screenheight() - 80) / scaling))
        self.geometry(f'{max(920, width)}x{max(620, height)}')
        self.minsize(920, 620)
        self.configure(fg_color=self.CREAM)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.events = queue.Queue()
        self.ready = None
        self.busy = False
        self.last_output = None
        self.log_lines = []
        self.started = None
        self.controls = []
        self.verified_sources = {}
        self.source_errors = {}
        self.validated_signatures = None
        self.active_job = None
        self.last_reference_refresh = 0
        self.reference_details = {}
        self.ntm_output_locations = ()
        self.pipedrive_downloaded_at = None
        self.status_icons = {name: self.make_status_icon(name) for name in ('ready', 'attention', 'working', 'pending')}
        self.vars = {key: tk.StringVar(value=value) for key, value in {
            'population': next(iter(POPULATIONS)), 'category': 'Please select',
            'ncop_scope': 'Please select', 'mode': 'Download via API',
            'population_mode': 'Download via API', 'population_file': '',
            'db': '', 'archive': '',
            'c3_drive': os.environ.get('C3_DRIVE_FOLDER_ID', backend.C3_DRIVE_FOLDER_ID),
            'c3': str(BASE / 'c3'), 'output': str(BASE / 'output'),
            'deals': '', 'persons': '', 'token': os.environ.get('PIPEDRIVE_API_TOKEN', '')}.items()}
        self.status = tk.StringVar(value='Complete the steps on the left, then click Check references.')
        self.build_layout()
        for key, var in self.vars.items():
            var.trace_add('write', lambda *_, key=key: self.inputs_changed(key))
        self.vars['population'].trace_add('write', self.population_changed)
        self.population_changed()
        self.mode_changed()
        self.refresh_references()
        self.protocol('WM_DELETE_WINDOW', self.close_app)
        self.after(100, self.poll)

    def label(self, parent, text='', size=13, bold=False, color=None, **kwargs):
        return ctk.CTkLabel(parent, text=text, text_color=color or self.NAVY,
                            font=(self.UI_FONT, size, 'bold' if bold else 'normal'), **kwargs)

    def button(self, parent, text, command, primary=False, **kwargs):
        widget = ctk.CTkButton(parent, text=text, command=command, height=34, corner_radius=9,
                              font=(self.UI_FONT, 12, 'bold'),
                              fg_color=self.ORANGE if primary else '#f0ecdf',
                              hover_color='#f5bb76' if primary else '#e5dfcf',
                              text_color=self.NAVY, text_color_disabled='#89928f',
                              **kwargs)
        self.controls.append((widget, 'normal'))
        return widget

    def card(self, parent, row, number, title, subtitle):
        card = ctk.CTkFrame(parent, fg_color=self.CARD, corner_radius=16,
                           border_width=1, border_color=self.LINE)
        card.grid(row=row, column=0, sticky='ew', padx=(0, 8), pady=(0, 14))
        card.grid_columnconfigure(0, weight=1)
        heading = ctk.CTkFrame(card, fg_color='transparent')
        heading.grid(row=0, column=0, sticky='ew', padx=18, pady=(16, 2))
        self.label(heading, number, 12, True, self.RED).pack(side='left', padx=(0, 9))
        self.label(heading, title, 17, True).pack(side='left')
        self.label(card, subtitle, 12, color=self.MUTED, anchor='w', justify='left', wraplength=460).grid(row=1, column=0, sticky='ew', padx=18, pady=(0, 12))
        fields = ctk.CTkFrame(card, fg_color='transparent')
        fields.grid(row=2, column=0, sticky='ew', padx=18, pady=(0, 16))
        fields.grid_columnconfigure(0, weight=1)
        return fields

    def entry(self, parent, key, **kwargs):
        widget = ctk.CTkEntry(parent, textvariable=self.vars[key], height=36,
                             corner_radius=9, fg_color='#ffffff', border_width=1,
                             border_color='#dadfdc', text_color=self.NAVY,
                             font=(self.UI_FONT, 12), **kwargs)
        self.controls.append((widget, 'normal'))
        return widget

    def path_row(self, parent, row, title, key, folder=False, db=False, open_button=False):
        self.label(parent, title, 12, True, anchor='w').grid(row=row, column=0, sticky='w', pady=(8, 5))
        strip = ctk.CTkFrame(parent, fg_color='transparent')
        strip.grid(row=row+1, column=0, sticky='ew')
        strip.grid_columnconfigure(0, weight=1)
        field = self.entry(strip, key)
        field.grid(row=0, column=0, sticky='ew', padx=(0, 8))
        def browse():
            selected = filedialog.askdirectory() if folder else filedialog.askopenfilename(
                filetypes=[('SQLite database', '*.db')] if db else [('CSV or Excel', '*.csv *.xlsx *.xls')])
            if selected:
                self.vars[key].set(selected)
        self.button(strip, 'Browse', browse, width=76).grid(row=0, column=1)
        if open_button:
            self.button(strip, 'Open', lambda: self.safe_open(self.vars[key].get()), width=60).grid(row=0, column=2, padx=(6, 0))
        return strip

    def source_label(self, parent, row):
        widget = self.label(parent, size=12, anchor='w', justify='left', wraplength=460)
        widget.grid(row=row, column=0, sticky='ew', pady=(10, 0))
        return widget

    def build_layout(self):
        header = ctk.CTkFrame(self, fg_color=self.NAVY, corner_radius=0)
        header.grid(row=0, column=0, sticky='ew')
        header.grid_columnconfigure(0, weight=1)
        self.label(header, 'Mail Marketing Cleanup', 25, True, self.CREAM, anchor='w').grid(row=0, column=0, sticky='w', padx=26, pady=(19, 0))
        self.label(header, 'An app that implements a population-cleaning workflow for mail marketing campaigns', 13, color=self.ORANGE, anchor='w').grid(row=1, column=0, sticky='w', padx=26, pady=(0, 18))

        self.credit_label = ctk.CTkLabel(
            header,
            text="© dyoliya • GitHub",
            text_color="#8a94a0",
            font=ctk.CTkFont(size=8, underline=False),
            cursor="hand2"
        )
        self.credit_label.place(relx=1.0, x=-10, y=1, anchor="ne")
        self.credit_label.bind("<Button-1>", lambda e: webbrowser.open("https://github.com/dyoliya"))

        body = ctk.CTkFrame(self, fg_color='transparent')
        body.grid(row=1, column=0, sticky='nsew', padx=22, pady=18)
        body.grid_columnconfigure(0, weight=3, minsize=460)
        body.grid_columnconfigure(1, weight=2, minsize=300)
        body.grid_rowconfigure(0, weight=1)
        self.form = ctk.CTkScrollableFrame(body, fg_color='transparent', corner_radius=0,
                                           scrollbar_button_color='#d3cbb8', scrollbar_button_hover_color='#b9b09b')
        self.form.grid(row=0, column=0, sticky='nsew', padx=(0, 14))
        self.form.grid_columnconfigure(0, weight=1)
        self.form.bind('<Configure>', self.fit_form_labels, add='+')

        self.api_frame = self.card(
            self.form, 0, '01', 'Connect to Pipedrive',
            ('Use API downloads or local exports. Enter your token when using API.'
             if ALLOW_MANUAL_PIPEDRIVE else
             'Enter your token once. Check references will prepare the Pipedrive deals and contacts automatically.'),
        )
        self.label(self.api_frame, 'API token', 12, True, anchor='w').grid(row=0, column=0, sticky='w', pady=(6, 5))
        self.api_entry = self.entry(self.api_frame, 'token', show='•')
        self.api_entry.grid(row=1, column=0, sticky='ew')
        self.button(self.api_frame, 'Open API token page', lambda: webbrowser.open(API_TOKEN_URL), width=170).grid(row=2, column=0, sticky='w', pady=(8, 0))
        self.mode_selector = self.option_menu(
            self.api_frame, 'mode', ('Download via API', 'Manual files')
        )
        self.mode_selector.grid(row=3, column=0, sticky='ew', pady=(10, 0))
        self.manual_frame = ctk.CTkFrame(self.api_frame, fg_color='transparent')
        self.manual_frame.grid(row=4, column=0, sticky='ew')
        self.manual_frame.grid_columnconfigure(0, weight=1)
        self.path_row(self.manual_frame, 0, 'Deals export', 'deals')
        self.path_row(self.manual_frame, 2, 'Persons export', 'persons')
        links = ctk.CTkFrame(self.manual_frame, fg_color='transparent')
        links.grid(row=4, column=0, sticky='w', pady=(10, 0))
        self.button(links, 'Open deals filter', lambda: webbrowser.open(DEALS_URL), width=130).pack(side='left', padx=(0, 8))
        self.button(links, 'Open persons filter', lambda: webbrowser.open(PERSONS_URL), width=145).pack(side='left')
        self.pipedrive_status = self.source_label(self.api_frame, 5)
        self.label(
            self.api_frame, 'Downloads under 8 hours old are reused.',
            11, color=self.MUTED, anchor='w',
        ).grid(row=6, column=0, sticky='w', pady=(4, 0))
        fields = self.card(self.form, 1, '02', 'Your population', 'Choose the source list to clean.')
        choices = ctk.CTkFrame(fields, fg_color='transparent')
        choices.grid(row=0, column=0, sticky='ew')
        choices.grid_columnconfigure(0, weight=3)
        choices.grid_columnconfigure(1, weight=2)
        self.label(choices, 'Population', 12, True, anchor='w').grid(row=0, column=0, sticky='w', pady=(0, 5))
        self.category_caption = self.label(choices, 'Category', 12, True, anchor='w')
        self.category_caption.grid(row=0, column=1, sticky='w', padx=(10, 0), pady=(0, 5))
        self.population_combo = self.option_menu(choices, 'population', tuple(POPULATIONS))
        self.population_combo.grid(row=1, column=0, sticky='ew')
        self.category_combo = self.option_menu(choices, 'category', ('Please select', *CATEGORIES), width=140,)
        self.category_combo.grid(row=1, column=1, sticky='ew', padx=(10, 0))
        self.ncop_scope_combo = self.option_menu(
            choices, 'ncop_scope', ('Please select', *backend.NCOP_SCOPES), width=150,
        )
        self.ncop_scope_combo.grid(row=1, column=1, sticky='ew', padx=(10, 0))
        self.ncop_scope_combo.grid_remove()
        self.ntm_locations_button = self.button(choices, 'Select Target counties / states ▾', self.choose_ntm_locations, width=190,)
        self.ntm_locations_button.grid(row=1, column=1, sticky='ew', padx=(10, 0))
        self.ntm_locations_button.grid_remove()
        self.population_db_frame = ctk.CTkFrame(fields, fg_color='transparent')
        self.population_db_frame.grid(row=1, column=0, sticky='ew')
        self.population_db_frame.grid_columnconfigure(0, weight=1)
        self.path_row(self.population_db_frame, 0, 'Population database', 'db', db=True)
        self.campaign_frame = ctk.CTkFrame(fields, fg_color='transparent')
        self.campaign_frame.grid(row=1, column=0, sticky='ew')
        self.campaign_frame.grid_columnconfigure(0, weight=1)
        self.population_source_caption = self.label(
            self.campaign_frame, 'Population source', 12, True, anchor='w'
        )
        self.population_source_caption.grid(row=0, column=0, sticky='w', pady=(10, 5))
        self.population_mode_selector = self.option_menu(
            self.campaign_frame, 'population_mode', ('Download via API', 'Manual files')
        )
        self.population_mode_selector.grid(row=1, column=0, sticky='ew')
        self.population_api_note = self.label(
            self.campaign_frame,
            'This population will be downloaded from Pipedrive when you check references.',
            12, color=self.MUTED, anchor='w', justify='left', wraplength=460,
        )
        self.population_api_note.grid(row=0, column=0, sticky='ew', pady=(10, 5))
        self.population_manual_frame = ctk.CTkFrame(self.campaign_frame, fg_color='transparent')
        self.population_manual_frame.grid(row=2, column=0, sticky='ew')
        self.population_manual_frame.grid_columnconfigure(0, weight=1)
        self.path_row(self.population_manual_frame, 0, 'Population export (CSV or Excel)', 'population_file')
        self.button(self.campaign_frame, 'Open population filter', self.open_population_filter, width=190).grid(row=3, column=0, sticky='w', pady=(8, 0))
        self.campaign_frame.grid_remove()
        self.db_status = self.source_label(fields, 3)
        fields = self.card(self.form, 2, '03', 'C3 references', 'Check Drive for the latest addresses, phones, and emails.')
        self.button(fields, 'Refresh latest C3 files', self.download_c3, primary=True).grid(row=0, column=0, sticky='ew')
        self.c3_status = self.source_label(fields, 1)
        self.c3_files_text = self.label(fields, size=11, color=self.MUTED, anchor='w', justify='left', wraplength=460)
        self.c3_files_text.grid(row=2, column=0, sticky='ew', pady=(5, 0))
        self.c3_options_button = self.button(fields, 'Folder settings  +', self.toggle_c3_settings, width=130)
        self.c3_options_button.grid(row=3, column=0, sticky='w', pady=(10, 0))
        self.c3_settings = ctk.CTkFrame(fields, fg_color='transparent')
        self.c3_settings.grid(row=4, column=0, sticky='ew')
        self.c3_settings.grid_columnconfigure(0, weight=1)
        self.path_row(self.c3_settings, 0, 'Local C3 folder', 'c3', folder=True, open_button=True)
        self.label(self.c3_settings, 'Google Drive folder link or ID', 12, True, anchor='w').grid(row=2, column=0, sticky='w', pady=(12, 5))
        self.entry(self.c3_settings, 'c3_drive').grid(row=3, column=0, sticky='ew')
        self.c3_settings.grid_remove()
        self.c3_settings_open = False

        # Pipedrive controls and status now belong to the top connection card.

        fields = self.card(
            self.form, 3, '04',
            'Archived deals',
            'Select the latest archived-deals export for cleanup matching.',
        )
        self.path_row(fields, 0, 'Archived-deals export', 'archive')
        self.archive_status = self.source_label(fields, 2)
        self.button(
            fields,
            'Open SharePoint archive',
            lambda: webbrowser.open(ARCHIVE_URL),
            width=185,
        ).grid(row=3, column=0, sticky='w', pady=(8, 4))

        fields = self.card(
            self.form, 4, '05',
            'Save your results',
            'Choose where to save the completed mail marketing cleanup.',
        )
        self.path_row(
            fields, 0, 'Results folder', 'output', folder=True
        )
        self.output_status = self.source_label(fields, 2)

        sidebar = ctk.CTkFrame(body, fg_color='transparent')
        sidebar.grid(row=0, column=1, sticky='nsew')
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(1, weight=1, minsize=150)
        summary = ctk.CTkFrame(sidebar, fg_color=self.CARD, corner_radius=16, border_width=1, border_color=self.LINE)
        summary.grid(row=0, column=0, sticky='ew', pady=(0, 14))
        summary.grid_columnconfigure(0, weight=1)
        self.label(summary, 'References', 18, True, anchor='w').grid(row=0, column=0, sticky='ew', padx=18, pady=(15, 0))
        self.checklist_title = self.label(summary, '0 of 5 ready', 12, color=self.MUTED, anchor='w', height=18)
        self.checklist_title.grid(row=0, column=1, sticky='e', padx=(0, 18), pady=(15, 0))
        self.checklist_labels = {}
        for row, key in enumerate(('pipedrive', 'db', 'c3', 'archive', 'output'), 2):
            label = self.label(summary, size=12, anchor='w', compound='left', padx=7, height=20)
            label.grid(row=row, column=0, columnspan=2, sticky='ew', padx=18, pady=(3, 10 if key == 'output' else 3))
            self.checklist_labels[key] = label
        self.readiness_hint = self.label(summary, 'Complete your references to continue.', 11, color=self.MUTED, anchor='w', justify='left', wraplength=280)
        # Keep detailed instructions beside their inputs; this summary stays short.
        self.readiness_hint.grid(row=7, column=0, columnspan=2, sticky='ew', padx=18)
        self.readiness_hint.grid_remove()

        log_card = ctk.CTkFrame(sidebar, fg_color=self.NAVY, corner_radius=16)
        log_card.grid(row=1, column=0, sticky='nsew')
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(2, weight=1)
        self.label(log_card, 'Activity', 17, True, self.CREAM, anchor='w').grid(row=0, column=0, sticky='w', padx=18, pady=(15, 0))
        self.activity_state = self.label(log_card, 'Waiting to start', 11, color=self.ORANGE, anchor='w')
        self.activity_state.grid(row=1, column=0, sticky='ew', padx=18, pady=(0, 7))
        self.log = ctk.CTkTextbox(log_card, fg_color=self.NAVY, text_color=self.CREAM,
                                 font=(self.LOG_FONT, 11), wrap='word', corner_radius=0,
                                 scrollbar_button_color='#516571', scrollbar_button_hover_color='#71828b', state='disabled')
        self.log.grid(row=2, column=0, sticky='nsew', padx=12, pady=(0, 14))

        footer = ctk.CTkFrame(self, fg_color=self.CARD, corner_radius=0, border_width=1, border_color=self.LINE)
        footer.grid(row=2, column=0, sticky='ew')
        footer.grid_columnconfigure(0, weight=1)
        self.status_label = self.label(footer, size=12, anchor='w', justify='left', textvariable=self.status, wraplength=300)
        self.status_label.grid(row=0, column=0, sticky='ew', padx=24, pady=(13, 6))
        self.progress = ctk.CTkProgressBar(footer, mode='indeterminate', height=4,
                                         fg_color=self.LINE, progress_color=self.ORANGE)
        self.progress.grid(row=1, column=0, sticky='ew', padx=24, pady=(0, 14))
        self.progress.set(0)
        actions = ctk.CTkFrame(footer, fg_color='transparent')
        actions.grid(row=0, column=1, rowspan=2, sticky='e', padx=(0, 24), pady=16)
        self.output_button = ctk.CTkButton(actions, text='Open results', command=lambda: self.safe_open(self.last_output), width=95, height=38, fg_color='transparent', text_color=self.NAVY, text_color_disabled='#98a29f', hover_color='#fff6de', state='disabled')
        self.output_button.pack(side='left', padx=(0, 8))
        self.check_button = ctk.CTkButton(actions, text='Check references', command=self.check, width=135, height=38, corner_radius=10, fg_color=self.NAVY, hover_color='#3e5564', text_color=self.CREAM, text_color_disabled='#b4bdb9')
        self.check_button.pack(side='left', padx=(0, 8))
        self.run_button = ctk.CTkButton(actions, text='Run cleanup  →', command=self.run, width=140, height=38, corner_radius=10, fg_color=self.ORANGE, hover_color='#f5bb76', text_color=self.NAVY, text_color_disabled='#8d9690', state='disabled', font=(self.UI_FONT, 13, 'bold'))
        self.run_button.pack(side='left')

    def option_menu(self, parent, key, values, width=180):
        menu = ctk.CTkOptionMenu(parent, variable=self.vars[key], values=list(values), height=36,
                                 width=width, corner_radius=9, fg_color='#edf0ed', button_color='#e0e6e0',
                                 button_hover_color='#d4ddd5', text_color=self.NAVY,
                                 dropdown_fg_color=self.CARD, dropdown_text_color=self.NAVY,
                                 dropdown_hover_color='#f2e9d6', font=(self.UI_FONT, 12))
        self.controls.append((menu, 'normal'))
        return menu

    def toggle_c3_settings(self):
        self.c3_settings_open = not self.c3_settings_open
        if self.c3_settings_open:
            self.c3_settings.grid()
        else:
            self.c3_settings.grid_remove()
        self.c3_options_button.configure(text='Folder settings  −' if self.c3_settings_open else 'Folder settings  +')

    def fit_form_labels(self, event):
        wrap = max(240, event.width - 60)
        for name in ('db_status', 'c3_status', 'c3_files_text', 'pipedrive_status', 'archive_status', 'output_status'):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(wraplength=wrap)

    def make_status_icon(self, state):
        size, scale = 18, 4
        image = Image.new('RGBA', (size*scale, size*scale))
        draw = ImageDraw.Draw(image)
        colors = {'ready': self.GREEN, 'attention': self.RED, 'working': self.ORANGE, 'pending': '#b77725'}
        draw.ellipse((2, 2, size*scale-2, size*scale-2), fill=colors[state])
        if state == 'ready':
            points = [(5, 9), (8, 12), (13, 6)]
        elif state == 'attention':
            points = [(6, 6), (12, 12)]
            draw.line([(6*scale, 12*scale), (12*scale, 6*scale)], fill='white', width=2*scale)
        else:
            points = [(9, 5), (9, 10)]
            draw.ellipse((8*scale, 12*scale, 10*scale, 14*scale), fill='white')
        draw.line([(x*scale, y*scale) for x,y in points], fill='white', width=2*scale, joint='curve')
        return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))


    def mode_changed(self):
        config = self.config_values()
        reference_api = config['mode'] == 'Download via API'
        population_api = (
            backend.is_campaign_population(config['population'])
            and config['population_mode'] == 'Download via API'
        )

        if ALLOW_MANUAL_PIPEDRIVE:
            self.mode_selector.grid()
            self.population_source_caption.grid()
            self.population_mode_selector.grid()
            self.population_api_note.grid_remove()
        else:
            self.mode_selector.grid_remove()
            self.population_source_caption.grid_remove()
            self.population_mode_selector.grid_remove()
            self.population_api_note.grid()

        if ALLOW_MANUAL_PIPEDRIVE and not reference_api:
            self.manual_frame.grid()
        else:
            self.manual_frame.grid_remove()

        if ALLOW_MANUAL_PIPEDRIVE and config['population_mode'] == 'Manual files':
            self.population_manual_frame.grid()
        else:
            self.population_manual_frame.grid_remove()

        self.api_entry.configure(
            state='normal' if (reference_api or population_api) and not self.busy else 'disabled'
        )


    def open_population_filter(self):
        population = self.vars['population'].get()
        if backend.is_campaign_population(population):
            webbrowser.open(backend.campaign_filter_url(population))


    def update_population_controls(self):
        population = self.vars['population'].get()
        settings = backend.population_settings(population)

        # Hide both filters before showing the applicable one.
        self.category_combo.grid_remove()
        self.ncop_scope_combo.grid_remove()
        self.ntm_locations_button.grid_remove()

        if settings['category']:
            self.category_caption.configure(text='Category')
            self.category_caption.grid()
            self.category_combo.grid()

        elif population == 'NCOP':
            self.category_caption.configure(text='Owner selection')
            self.category_caption.grid()
            self.ncop_scope_combo.grid()

        elif population == backend.NTM_POPULATION:
            self.category_caption.configure(text='Output counties / states')
            self.category_caption.grid()
            self.ntm_locations_button.grid()

        else:
            self.category_caption.grid_remove()

        self.category_combo.configure(
            state='normal'
            if settings['category'] and not self.busy
            else 'disabled'
        )

        self.ncop_scope_combo.configure(
            state='normal'
            if population == 'NCOP' and not self.busy
            else 'disabled'
        )

        count = len(self.ntm_output_locations)
        self.ntm_locations_button.configure(
            text=f'{count} selected ▾' if count else 'Select counties / states ▾',
            state='normal' if population == backend.NTM_POPULATION and not self.busy else 'disabled',
        )

        campaign = backend.is_campaign_population(population)
        if campaign:
            self.population_db_frame.grid_remove()
            self.campaign_frame.grid()
        else:
            self.campaign_frame.grid_remove()
            self.population_db_frame.grid()

        supported = bool(settings['validate'] and settings['execute'])
        supplied = (
            bool(self.reference_details)
            and all(v[0] for v in self.reference_details.values())
        )
        self.check_button.configure(
            state='normal'
            if supported and supplied and not self.busy
            else 'disabled'
        )


    def choose_ntm_locations(self):
        if self.busy:
            return
        path = self.vars['db'].get().strip()
        if not path or not Path(path).is_file():
            messagebox.showinfo('Select the database', 'Select your NTM SQLite database first.')
            return
        self.add_log('Loading NTM county/state choices from eligible source rows...')
        self.launch('ntm_locations', lambda: backend.ntm_output_locations(path))

    def show_ntm_locations(self, options):
        if not options:
            messagebox.showinfo('No locations', 'No eligible NTM rows have both county and state.')
            return
        popup = ctk.CTkToplevel(self)
        popup.title('NTM output counties / states')
        popup.geometry('520x600')
        popup.minsize(420, 420)
        popup.configure(fg_color=self.CREAM)
        popup.transient(self)
        popup.grid_columnconfigure(0, weight=1)
        popup.grid_rowconfigure(3, weight=1)
        self.label(
            popup, 'Choose locations to include in the output.', 15, True, anchor='w'
        ).grid(row=0, column=0, sticky='ew', padx=20, pady=(18, 6))
        self.label(
            popup, 'All eligible NTM rows are still matched. Group exclusions apply across counties.',
            12, color=self.MUTED, anchor='w', justify='left', wraplength=460,
        ).grid(row=1, column=0, sticky='ew', padx=20, pady=(0, 12))
        search = tk.StringVar()
        ctk.CTkEntry(popup, textvariable=search, placeholder_text='Search county or state').grid(
            row=2, column=0, sticky='ew', padx=20, pady=(0, 10)
        )
        listing = ctk.CTkScrollableFrame(popup, fg_color=self.CARD)
        listing.grid(row=3, column=0, sticky='nsew', padx=20)
        listing.grid_columnconfigure(0, weight=1)
        selected = set(self.ntm_output_locations)
        choices = {}
        boxes = {}
        for number, pair in enumerate(options):
            variable = tk.BooleanVar(value=pair in selected)
            choices[pair] = variable
            box = ctk.CTkCheckBox(
                listing, text=f'{pair[0]}, {pair[1]}', variable=variable,
                text_color=self.NAVY, fg_color=self.GREEN, hover_color='#1e5c3c',
            )
            box.grid(row=number, column=0, sticky='w', padx=8, pady=6)
            boxes[pair] = box
        count_text = tk.StringVar()

        def update_count(*_):
            count_text.set(f'{sum(value.get() for value in choices.values())} selected')

        def filter_options(*_):
            query = search.get().strip().upper()
            for pair, box in boxes.items():
                if query in f'{pair[0]}, {pair[1]}':
                    box.grid()
                else:
                    box.grid_remove()

        def select_all():
            for value in choices.values():
                value.set(True)

        def clear_all():
            for value in choices.values():
                value.set(False)

        def apply():
            selection = tuple(pair for pair, value in choices.items() if value.get())
            if not selection:
                messagebox.showinfo('Choose a location', 'Select at least one county/state.', parent=popup)
                return
            self.ntm_output_locations = selection
            self.inputs_changed('ntm_output_locations')

            self.add_log(
                f'NTM target county/state selected ({len(selection)}):\n'
                + '\n'.join(
                    f'  - {county}, {state}'
                    for county, state in selection
                )
            )

            self.add_log(
                'Matching scope: ALL eligible NTM rows, '
                'including counties outside the selected target county/state.'
            )

            popup.destroy()

        search.trace_add('write', filter_options)
        for value in choices.values():
            value.trace_add('write', update_count)
        update_count()
        footer = ctk.CTkFrame(popup, fg_color='transparent')
        footer.grid(row=4, column=0, sticky='ew', padx=20, pady=16)
        ctk.CTkLabel(footer, textvariable=count_text, text_color=self.NAVY).pack(side='left')
        # Direct CTkButtons: popup widgets must not enter self.controls, because
        # those widgets are destroyed when the popup closes.
        ctk.CTkButton(footer, text='All', width=45, command=select_all).pack(side='left', padx=(8, 4))
        ctk.CTkButton(footer, text='Clear', width=55, command=clear_all).pack(side='left')
        ctk.CTkButton(
            footer, text='Apply', width=80, command=apply,
            fg_color=self.GREEN, hover_color='#1e5c3c',
        ).pack(side='right')
        popup.after(100, popup.grab_set)

    def refresh_references(self):
        self.last_reference_refresh = time.monotonic()
        details, signatures = self.inspect_references()
        self.reference_details = details
        if self.ready is not None and self.validated_signatures != signatures:
            self.invalidate()
            self.validated_signatures = None
            self.status.set('An input changed. Check references again before running.')
        if (self.ready is not None or 'pipedrive' in self.verified_sources) and self.vars['mode'].get() == 'Download via API':
            downloaded_at = self.pipedrive_downloaded_at
            if downloaded_at is None or not 0 <= time.time() - downloaded_at < 8*60*60:
                self.invalidate()
                self.validated_signatures = None
                self.verified_sources.pop('pipedrive', None)
                self.status.set('Pipedrive downloads expired. Check references to refresh them.')
        if (self.ready is not None
                and backend.is_campaign_population(self.vars['population'].get())
                and self.vars['population_mode'].get() == 'Download via API'):
            stamp = self.ready['population_frame'].attrs.get('population_downloaded_at')
            if stamp is None or not 0 <= time.time()-stamp < 8*60*60:
                self.invalidate()
                self.validated_signatures = None
                self.verified_sources.pop('db', None)
                self.status.set('Population download expired. Check references to refresh it.')
        labels = {'pipedrive': 'Pipedrive', 'db': 'Population list', 'c3': 'C3 files', 'archive': 'Archived deals', 'output': 'Results folder'}
        inline = {'db': self.db_status, 'c3': self.c3_status, 'archive': self.archive_status,
                  'pipedrive': self.pipedrive_status, 'output': self.output_status}
        ready_count, supplied = 0, 0
        for key, label in labels.items():
            available, description = details[key]
            supplied += bool(available)
            verified = self.verified_sources.get(key)
            failure = self.source_errors.get(key)
            if verified and verified[0] != signatures[key]:
                self.verified_sources.pop(key, None)
                verified = None
            if failure and failure[0] != signatures[key]:
                self.source_errors.pop(key, None)
                failure = None
            if self.busy and ((self.active_job == 'c3_downloaded' and key == 'c3') or self.active_job == 'ready'):
                state, badge, color = 'working', 'Checking', self.MUTED
                description = 'Checking the latest files...' if key == 'c3' else 'Checking your references...'
            elif failure:
                state, badge, color = 'attention', 'Needs attention', self.RED
                description = failure[1]
            elif verified and available:
                state, badge, color = 'ready', 'Ready', self.GREEN
                description = verified[1]
                ready_count += 1
            elif available:
                state, badge, color = 'pending', 'Check required', '#98651d'
            else:
                state, badge, color = 'attention', 'Needed', self.RED
            self.checklist_labels[key].configure(text=f'{label}   ·   {badge}', text_color=color, image=self.status_icons[state])
            inline[key].configure(text=description, text_color=color, image=self.status_icons[state], compound='left', padx=5)
        # Long filenames stay in the form; the readiness summary remains compact.
        self.c3_files_text.configure(text=self.c3_filenames if details['c3'][0] else 'Refresh C3 to confirm the latest Drive copies.')
        runnable = self.ready is not None and not self.busy
        self.run_button.configure(state='normal' if runnable else 'disabled')
        self.checklist_title.configure(text=f'{ready_count} of 5 ready', text_color=self.GREEN if runnable else self.MUTED)
        if runnable:
            hint = 'Everything is verified. You can run cleanup.'
        elif self.busy:
            hint = 'Work is in progress. Follow the activity below.'
        elif supplied == 5:
            hint = 'Inputs are supplied. Click Check references.'
        else:
            missing = [labels[key] for key,v in details.items() if not v[0]]
            hint = 'Next: ' + ', '.join(missing) + '.'
        self.readiness_hint.configure(text=hint)
        self.update_population_controls()


    def safe_open(self, path):
        try:
            if path:
                open_folder(path)
        except Exception as error:
            messagebox.showerror('Could not open folder', str(error))


    def invalidate(self, *_):
        self.ready = None
        self.run_button.configure(state='disabled')
        self.status.set('Inputs changed. Click Check references to validate.')


    def population_changed(self, *_):
        self.vars['population_file'].set('')
        self.update_population_controls()
        self.mode_changed()
        settings = backend.population_settings(self.vars['population'].get())
        if not settings['category']:
            self.vars['category'].set('')
        elif not self.vars['category'].get():
            self.vars['category'].set('Please select')
        if not settings['execute']:
            self.status.set('This population is listed for future use. Its cleanup rules and column mapping are pending.')


    def inputs_changed(self, key):
        if key == 'db':
            self.ntm_output_locations = ()
        self.invalidate()
        self.validated_signatures = None
        related = {'db': 'db', 'category': 'db', 'ncop_scope': 'db', 'population': 'db',
                   'ntm_output_locations': 'db', 'population_mode': 'db', 'population_file': 'db',
                   'c3': 'c3', 'c3_drive': 'c3', 'archive': 'archive',
                   'mode': 'pipedrive', 'token': 'pipedrive', 'deals': 'pipedrive',
                   'persons': 'pipedrive', 'output': 'output'}.get(key)
        if related:
            self.source_errors.pop(related, None)
        if key in ('mode', 'population_mode'):
            self.mode_changed()
        self.refresh_references()


    @staticmethod
    def file_signature(value):
        if not value:
            return None
        path = Path(value)
        try:
            stat = path.stat()
            return (str(path.resolve()), stat.st_size, stat.st_mtime_ns, path.is_file())
        except OSError:
            return (str(path), None, None, False)


    def inspect_references(self):
        """Summarize supplied inputs; backend checking proves their contents."""
        config = self.config_values()
        settings = backend.population_settings(config['population'])
        database = self.file_signature(config['db'])
        archive = self.file_signature(config['archive'])
        signatures = {
            'db': (backend.selection_signature(config),
                   database, self.file_signature(config['db'] + '-wal') if config['db'] else None),
            'archive': archive,
            'output': config['output'],
        }

        selection_ok = (
            config['category'] in CATEGORIES if settings['category'] else
            config['ncop_scope'] in backend.NCOP_SCOPES if config['population'] == 'NCOP' else
            bool(config['ntm_output_locations']) if config['population'] == backend.NTM_POPULATION else
            True
        )
        db_supplied = bool(
            database and database[-1] and settings['validate'] and selection_ok
        )
        details = {
            'db': (db_supplied, (Path(config['db']).name if db_supplied else 'Select your population database and the applicable filter.')
                   if settings['validate'] else 'Cleanup rules for this population are pending.'),
            'archive': (bool(archive and archive[-1]), Path(config['archive']).name if archive and archive[-1] else 'Select the latest archived-deals export.'),
            'output': (bool(config['output']), config['output'] or 'Choose where results will be saved.'),
        }
        if backend.is_campaign_population(config['population']):
            local = self.file_signature(config['population_file'])
            api = config['population_mode'] == 'Download via API'
            signatures['db'] = (backend.selection_signature(config), None if api else local)
            available = bool(config['token']) if api else bool(local and local[-1])
            details['db'] = (available,
                f"Population API filter {backend.CAMPAIGN_FILTERS[config['population']]} selected — check references."
                if api and available else
                'Enter the shared API token above for the population download.' if api else
                Path(config['population_file']).name if available else 'Select the population export.')
        try:
            files = backend.c3_files(config['c3'])
            signatures['c3'] = (
                config['c3'],
                config['c3_drive'],
                tuple(
                    (kind, self.file_signature(str(path)))
                    for kind, path in sorted(files.items())
                ),
            )

            verified = self.verified_sources.get('c3')
            refreshed = bool(
                verified and verified[0] == signatures['c3']
            )

            details['c3'] = (
                refreshed,
                'Latest C3 files checked and verified.'
                if refreshed else
                'Click Refresh latest C3 files to refresh your references.',
            )

            names = '\n'.join(
                f'{kind}: {path.name}'
                for kind, path in files.items()
            )
            self.c3_filenames = (
                names if refreshed else
                'Existing local copies — refresh required:\n' + names
            )

        except (ValueError, OSError) as error:
            signatures['c3'] = (
                config['c3'], config['c3_drive'], str(error)
            )
            details['c3'] = (
                False,
                'Click Refresh latest C3 files to get all three references.',
            )
            self.c3_filenames = str(error)
        if config['mode'] == 'Download via API':
            signatures['pipedrive'] = ('api', config['token'])
            details['pipedrive'] = (
                bool(config['token']),
                'Token entered — click Check references to prepare Pipedrive.'
                if config['token'] else
                ('Choose API or manual reference exports in the Pipedrive section above.'
                 if ALLOW_MANUAL_PIPEDRIVE else
                 'Enter your API token in Connect to Pipedrive above.'),
            )
        else:
            deals, persons = self.file_signature(config['deals']), self.file_signature(config['persons'])
            signatures['pipedrive'] = ('manual', deals, persons)
            missing = [name for name, signature in (('deals', deals), ('persons', persons)) if not signature or not signature[-1]]
            details['pipedrive'] = (not missing, 'Both exports selected — content check required.' if not missing else 'Select the ' + ' and '.join(missing) + ' export' + ('s.' if len(missing) > 1 else '.'))
        return details, signatures


    def config_values(self):
        config = {key: var.get().strip() for key, var in self.vars.items()}
        config['ntm_output_locations'] = self.ntm_output_locations
        if not ALLOW_MANUAL_PIPEDRIVE:
            config['mode'] = 'Download via API'
            config['population_mode'] = 'Download via API'
        return config


    def set_busy(self, value):
        self.busy = value
        self.activity_state.configure(text='Working · elapsed time below' if value else f'Idle · {len(self.log_lines):,} messages')
        for widget, normal in self.controls:
            widget.configure(state='disabled' if value else normal)
        self.update_population_controls()
        self.mode_changed()
        self.run_button.configure(state='disabled')
        if value:
            self.started = time.monotonic()
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.set(0)
            self.started = None


    def launch(self, kind, job):
        self.active_job = kind
        self.set_busy(True)
        self.refresh_references()
        self.status_label.configure(text_color=self.NAVY)
        self.status.set({'ready': 'Checking inputs and downloading references...', 'c3_downloaded': 'Downloading latest C3 files from Google Drive...', 'ntm_locations': 'Loading NTM county/state choices...'}.get(kind, 'Cleanup is running...'))
        def work():
            writer = ActivityWriter(self.events)
            with contextlib.redirect_stdout(writer):
                try:
                    result = job()
                    writer.flush()
                    self.events.put((kind, result))
                except Exception as error:
                    writer.flush()
                    # Never print request URLs, headers, or credentials.
                    message = str(error) or (
                        f'{type(error).__name__} (no message)\n{traceback.format_exc()}'
                    )
                    self.events.put(('error', message))
        threading.Thread(target=work, daemon=True).start()


    def download_c3(self):
        if self.busy:
            return
        folder_link = self.vars['c3_drive'].get().strip()
        destination = self.vars['c3'].get().strip()
        if not folder_link or not destination:
            messagebox.showinfo('Complete the C3 inputs', 'Enter the Google Drive folder link or ID and select a local C3 folder.')
            return
        self.invalidate()
        self.source_errors.pop('c3', None)
        self.verified_sources.pop('c3', None)
        self.launch(
            'c3_downloaded',
            lambda: backend.download_latest_c3(folder_link, destination),
        )


    def check(self):
        if self.busy:
            return

        self.add_log('CHECK REFERENCES: starting input checks...')

        self.ready = None
        self.validated_signatures = None
        self.source_errors.clear()
        self.last_output = None
        self.output_button.configure(
            state='disabled',
            fg_color='transparent',
            text_color=self.NAVY,
            hover_color=self.CREAM,
        )
        self.refresh_references()

        if not self.reference_details['c3'][0]:
            self.add_log(
                'ACTION NEEDED: refresh the latest C3 files first.'
            )
            messagebox.showinfo(
                'Refresh C3 first',
                'Click Refresh latest C3 files before checking the inputs.',
            )
            return

        config = self.config_values()

        campaign = backend.is_campaign_population(config['population'])
        required = ['archive', 'c3', 'output']
        if campaign:
            if config['population_mode'] == 'Manual files':
                required.append('population_file')
        else:
            required.append('db')
        if config['mode'] == 'Manual files':
            required += ['deals', 'persons']
        if config['mode'] == 'Download via API' or (campaign and config['population_mode'] == 'Download via API'):
            required.append('token')

        names = {
            'db': 'population database',
            'population_file': 'population export',
            'archive': 'archived-deals export',
            'c3': 'local C3 folder',
            'output': 'results folder',
            'token': 'Pipedrive API token',
            'deals': 'Pipedrive deals export',
            'persons': 'Pipedrive persons export',
        }

        missing = [names[key] for key in required if not config[key]]

        if missing:
            message = 'Please provide: ' + ', '.join(missing)
            self.add_log('ACTION NEEDED: ' + message)
            messagebox.showinfo('Complete the inputs', message)
            return

        self.add_log(f'Population: {config["population"]}')

        if config['population'] == 'NCOP':
            self.add_log(f'Owner selection: {config["ncop_scope"]}')
        elif config['population'] == backend.NTM_POPULATION:
            self.add_log('NTM target county/state: ' + '; '.join(
                f'{county}, {state}' for county, state in config['ntm_output_locations']
            ))
            self.add_log('Matching scope: ALL eligible NTM rows, including other counties.')
        elif config['category']:
            self.add_log(f'Category: {config["category"]}')


        if campaign:
            self.add_log(f'Population source: {config["population_mode"]}')
            self.add_log(f'Population filter: {backend.CAMPAIGN_FILTERS[config["population"]]}')
        self.add_log('C3 refresh confirmed.')
        self.add_log(f'Pipedrive source: {config["mode"]}')
        self.add_log(
            'Starting backend validation and reference loading. '
            'Large files may take a few minutes.'
        )

        self.launch('ready', lambda: backend.prepare(config))


    def run(self):
        if self.ready is None or self.busy:
            return
        config, ready = self.config_values(), self.ready
        def job():
            return backend.run_cleanup(
                config, ready,
                on_folder=lambda folder: self.events.put(('folder', folder)),
            )
        self.launch('done', job)


    def add_log(self, line):
        line = f'[{datetime.now():%H:%M:%S}] {line}'
        self.log_lines.append(line)
        if not self.busy:
            self.activity_state.configure(text=f'{len(self.log_lines):,} updates')
        self.log.configure(state='normal')
        self.log.insert('end', line + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')


    def poll(self):
        refresh_needed = False
        for _ in range(100):
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self.add_log(data)
            elif kind == 'ntm_locations':
                self.active_job = None
                self.set_busy(False)
                refresh_needed = True
                self.status.set('Choose the NTM counties/states to include in the output.')
                self.after(0, lambda options=data: self.show_ntm_locations(options))
            elif kind == 'folder':
                self.last_output = data
            elif kind == 'c3_downloaded':
                self.active_job = None
                details, signatures = self.inspect_references()
                self.verified_sources['c3'] = (signatures['c3'], f'Checked against Drive at {datetime.now():%H:%M}')
                self.source_errors.pop('c3', None)
                self.set_busy(False)
                refresh_needed = True
                self.ready = None
                self.status.set('Latest C3 files checked. Click Check references before running cleanup.')
                self.add_log('C3 refresh complete: ' + ', '.join(Path(path).name for path in data.values()))
                messagebox.showinfo('C3 files are ready', 'All three C3 files were checked against Drive and validated locally.\n\n' + '\n'.join(Path(path).name for path in data.values()) + '\n\nComplete the remaining checklist items, then click Check references.')
            elif kind == 'ready':
                self.active_job = None
                details, signatures = self.inspect_references()
                self.validated_signatures = signatures
                self.verified_sources = {key: (signature, 'Verified') for key, signature in signatures.items()}
                if self.vars['mode'].get() == 'Download via API':
                    self.pipedrive_downloaded_at = data['deals'].attrs.get('pipedrive_downloaded_at')
                    self.verified_sources['pipedrive'] = (signatures['pipedrive'], f'Deals and persons ready: {len(data["deals"]):,} deals, {len(data["persons"]):,} persons')
                self.set_busy(False)
                refresh_needed = True
                self.ready = data
                self.run_button.configure(state='normal')
                self.status.set(f'Ready: {data["count"]:,} records. References checked at {data["prepared_at"]}.')
                self.add_log('All required inputs passed. Run cleanup is now available.')
            elif kind == 'done':
                self.active_job = None
                self.set_busy(False)
                refresh_needed = True
                self.ready = None
                self.output_button.configure(
                    state='normal',
                    fg_color=self.GREEN,
                    text_color='#ffffff',
                    hover_color='#1e5c3c',
                )
                self.status.set(f'Complete — {data["output_rows"]:,} rows saved. Clean Y: {data["clean_Y"]:,} | Excluded N: {data["excluded_N"]:,}')
                self.save_log()
                messagebox.showinfo('Cleanup complete', f'Results saved to:\n{self.last_output}\n\nClick Open results to view your files.')
            elif kind == 'error':
                if self.active_job == 'c3_downloaded':
                    _, signatures = self.inspect_references()
                    self.source_errors['c3'] = (signatures['c3'], 'Download failed — resolve the error or check your local files.')
                    self.verified_sources.pop('c3', None)
                self.active_job = None
                self.validated_signatures = None
                self.set_busy(False)
                refresh_needed = True
                self.ready = None
                self.status.set('Could not continue. See the activity log and check the inputs again.')
                self.status_label.configure(text_color=self.RED)
                self.add_log('ACTION NEEDED: ' + data)
                self.save_log()
                messagebox.showerror('Action needed', data)
        if refresh_needed or time.monotonic() - self.last_reference_refresh >= 2:
            self.refresh_references()
        if self.busy and self.started:
            self.status.set(f'Working • elapsed {int(time.monotonic() - self.started)} seconds • activity continues below')
        self.after(100, self.poll)


    def save_log(self):
        if self.last_output:
            try:
                (self.last_output / 'activity.log').write_text('\n'.join(self.log_lines), encoding='utf-8')
            except OSError:
                self.add_log('Could not save the activity log. The log remains visible here.')


    def close_app(self):
        if self.busy:
            messagebox.showinfo('Work in progress', 'Please wait for this operation to finish before closing the app.')
            return
        self.destroy()



if __name__ == '__main__':
    CleanupApp().mainloop()
