import unittest
import unittest.mock

from infofilm import prompt_styles
from infofilm.image_recovery import generation_prompt


def case(style=None):
    asset = {'id': 'A1', 'visual_style': 'An oil painting with visible brushstrokes and muted earth tones.'}
    value = {'asset_id': 'A1', 'library': {'assets': [asset]},
             'message_annotation': {'setting': 'East African river valley, about 1.8 million years ago'}}
    if style:
        value['prompt_style'] = style
    return value


class PromptStyleTests(unittest.TestCase):
    def test_structured_prompts_keep_their_labelled_blocks(self):
        prompt = prompt_styles.compose('structured', setting='S', visual_style='V', layout='L', scene='X', addendum='+A')
        self.assertEqual(prompt, 'Setting: S\nVisual treatment: V\nComposition: L\nScene: X+A')

    def test_descriptive_prompts_are_one_paragraph_led_by_the_medium(self):
        prompt = prompt_styles.compose('descriptive_v1', setting='East African river valley.',
                                       visual_style='An oil painting with visible brushstrokes.',
                                       layout='Natural full-frame composition.', scene='A Homo erectus kneels.')
        self.assertEqual(prompt, 'An oil painting with visible brushstrokes. A Homo erectus kneels. '
                                 'East African river valley. Natural full-frame composition.')
        self.assertNotIn('Setting:', prompt)
        self.assertNotIn('\n', prompt)

    def test_generation_prompt_follows_the_case_style(self):
        with unittest.mock.patch('infofilm.image_recovery.layout_direction', return_value='Natural full-frame composition.'), \
             unittest.mock.patch('infofilm.person_references.reference_addendum', return_value=''):
            self.assertTrue(generation_prompt(case(), 'A Homo erectus kneels.').startswith('Setting: '))
            self.assertTrue(generation_prompt(case('descriptive_v1'), 'A Homo erectus kneels.').startswith('An oil painting'))

    def test_unknown_styles_are_refused(self):
        self.assertEqual(prompt_styles.prompt_style({}), 'structured')
        with self.assertRaises(ValueError):
            prompt_styles.prompt_style({'image_prompt_style': 'flux'})


if __name__ == '__main__':
    unittest.main()
