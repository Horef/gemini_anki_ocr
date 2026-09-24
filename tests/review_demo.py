"""Manual UI smoke test with synthetic notes. Never calls Gemini or Anki."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ankigen.review_window import review_dialog

plan = {'deck': 'Demo — no Anki changes', 'status': {}, 'notes': [
    {'note_id': 123, 'fields': {'Question': 'What is the role of myelin?',
                              'Answer': 'It insulates axons.'}}], 'actions': [
    {'action': 'update', 'note_id': 123, 'reason': 'Adds how insulation affects conduction.',
     'evidence': 'The demonstration excerpt describes saltatory conduction.',
     'fields': {'Question': 'What is the role of myelin?',
                'Answer': '<div>It insulates axons.</div><div>It helps action potentials propagate between nodes.</div>'}},
    {'action': 'create', 'reason': 'A separate retrieval target.', 'evidence': 'Synthetic example.',
     'fields': {'Question': 'Where are the gaps in the myelin sheath?',
                'Answer': 'At the nodes of Ranvier.'}},
    {'action': 'review', 'reason': 'Conflicting claims need clarification.',
     'evidence': 'This is a synthetic conflict, not a real note.'}]}
if __name__ == '__main__':
    print(review_dialog(plan, 'Demo preview', 1))
