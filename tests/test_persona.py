import tempfile
import unittest
from hermes_soul import prompts
from hermes_soul.runtime import Runtime


class PersonaTests(unittest.TestCase):
    def test_every_prompt_token_is_filled(self):
        cfg = {'persona': {'name': '阿青', 'owner': '他', 'interests': '咖啡'}}
        for name in ('IDENTITY_CONTRACT', 'CHOOSE', 'REMEMBER', 'CHAT', 'EXPRESSION_REVIEW', 'REACH_OUT', 'PUBLIC_PERSONA'):
            text = prompts.render(getattr(prompts, name), cfg)
            self.assertNotIn('<<', text, name)
        self.assertIn('你是阿青', prompts.render(prompts.IDENTITY_CONTRACT, cfg))
        self.assertIn('咖啡', prompts.render(prompts.PUBLIC_PERSONA, cfg))
        self.assertIn('TA', prompts.render(prompts.REACH_OUT))

    def test_host_text_uses_configured_owner(self):
        with tempfile.TemporaryDirectory() as home:
            r = Runtime({'home': home, 'mcp_url': 'http://127.0.0.1:1/mcp', 'interval_seconds': 720,
                         'max_calls_per_day': 10, 'delivery': {'mode': 'draft'},
                         'persona': {'name': '阿青', 'owner': '老王'}})
            try:
                self.assertEqual(r.status()['persona_name'], '阿青')
                self.assertIn('老王没有看到', r.contact.state()['delivery_note'])
            finally:
                r.store.close()


if __name__ == '__main__':
    unittest.main()
