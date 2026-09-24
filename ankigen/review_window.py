"""Local review window. No network access or Anki writes happen in this module."""
import html
import re


def readable(value):
    value = re.sub(r'<img\b[^>]*>', lambda m: '\n[Image: ' + html.escape(m.group(0)) + ']\n', value, flags=re.I)
    value = re.sub(r'<(?:br\s*/?|/div|/p|/li)>', '\n', value, flags=re.I)
    # Preserve the image reference as text, while removing formatting tags.
    value = re.sub(r'<[^>]+>', '', value)
    return html.unescape(value).strip()


def details(plan, index):
    a = plan['actions'][index]
    parts = [a['action'].upper(), a['reason'], '\nSource evidence:\n' + a['evidence']]
    if a.get('note_id'):
        parts.append(f"\nExisting note: {a['note_id']}")
    old = next((n for n in plan['notes'] if n['note_id'] == a.get('note_id')), None)
    for field, value in a.get('fields', {}).items():
        before = old['fields'].get(field) if old else None
        parts.append('\n' + field.upper())
        if before is not None and before != value:
            parts.append('BEFORE\n' + readable(before) + '\n\nAFTER\n' + readable(value))
        else:
            parts.append(readable(value) + ('\n(unchanged)' if before is not None else ''))
    if a['action'] == 'create':
        parts.append('\nTags: ' + ', '.join(sorted(set(a.get('tags', []) + ['gemini_auto']))))
    if a['action'] == 'review':
        parts.append('\nThis item needs clarification. It cannot be applied automatically.')
    return '\n'.join(parts)


def review_dialog(plan, run_name, queued):
    """Return (apply|discard|later, selected indices). Closing always means Later."""
    import tkinter as tk
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()
    root.title('Review Anki notes')
    root.geometry('1080x740')
    root.minsize(780, 520)
    result = ('later', [])
    visible = [i for i, a in enumerate(plan['actions'])
               if a['action'] != 'skip' and plan['status'].get(str(i)) not in {'done', 'dismissed'}]
    eligible = {i for i in visible if plan['actions'][i]['action'] in {'create', 'update'}}
    selected = set(eligible)
    header = ttk.Frame(root, padding=16)
    header.pack(fill='x')
    ttk.Label(header, text=plan['deck'], font=('', 19, 'bold')).pack(anchor='w')
    ttk.Label(header, text=f'{len(visible)} items · {queued} queued batches · {run_name}').pack(anchor='w')
    panes = ttk.Panedwindow(root, orient='horizontal')
    panes.pack(fill='both', expand=True, padx=16)
    left, right = ttk.Frame(panes), ttk.Frame(panes)
    panes.add(left, weight=1); panes.add(right, weight=3)
    tree = ttk.Treeview(left, columns=('include', 'kind'), show='tree headings', selectmode='browse')
    tree.heading('#0', text='Note'); tree.heading('include', text='Apply'); tree.heading('kind', text='Action')
    tree.column('#0', width=190); tree.column('include', width=45, stretch=False)
    tree.column('kind', width=65, stretch=False)
    scroll = ttk.Scrollbar(left, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y'); tree.pack(fill='both', expand=True)
    body = ScrolledText(right, wrap='word', font=('', 14), padx=16, pady=12)
    body.pack(fill='both', expand=True)
    for i in visible:
        a = plan['actions'][i]
        title = a.get('fields', {}).get('Front') or a.get('fields', {}).get('Question') or a['reason']
        tree.insert('', 'end', iid=str(i), text=readable(title),
                    values=('✓' if i in selected else '—', a['action']))

    def show(event=None):
        row = tree.selection()
        if row:
            body.configure(state='normal'); body.delete('1.0', 'end')
            body.insert('1.0', details(plan, int(row[0]))); body.configure(state='disabled')

    def toggle(index):
        if index in eligible:
            if index in selected:
                selected.remove(index)
            else:
                selected.add(index)
            tree.set(str(index), 'include', '✓' if index in selected else '')

    def click(event):
        row = tree.identify_row(event.y)
        if row and tree.identify_column(event.x) == '#1':
            toggle(int(row))

    def space(event):
        if tree.selection():
            toggle(int(tree.selection()[0]))
        return 'break'

    def choose_all(include):
        for i in eligible:
            if (i in selected) != include:
                toggle(i)

    def finish(choice):
        nonlocal result
        result = (choice, sorted(selected) if choice == 'apply' else [])
        root.destroy()

    tree.bind('<<TreeviewSelect>>', show)
    tree.bind('<ButtonRelease-1>', click)
    tree.bind('<space>', space)
    footer = ttk.Frame(root, padding=16); footer.pack(fill='x')
    ttk.Label(footer, text='Apply selected & finish also discards unchecked proposals. Recovery records are archived.').pack(anchor='w')
    ttk.Label(footer, text='Later keeps this batch unchanged. No extra Gemini calls.').pack(anchor='w', pady=(0, 10))
    ttk.Button(footer, text='Select all', command=lambda: choose_all(True)).pack(side='left')
    ttk.Button(footer, text='Select none', command=lambda: choose_all(False)).pack(side='left', padx=6)
    ttk.Button(footer, text='Apply selected & finish', command=lambda: finish('apply')).pack(side='right')
    ttk.Button(footer, text='Discard remaining', command=lambda: finish('discard')).pack(side='right', padx=6)
    ttk.Button(footer, text='Later', command=lambda: finish('later')).pack(side='right')
    root.protocol('WM_DELETE_WINDOW', lambda: finish('later'))
    root.bind('<Escape>', lambda e: finish('later'))
    if visible:
        tree.selection_set(str(visible[0])); tree.focus(str(visible[0])); show()
    root.lift(); root.attributes('-topmost', True)
    root.after(300, lambda: root.attributes('-topmost', False))
    tree.focus_force()
    root.mainloop()
    return result
