import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from infofilm.research_digest import bound_messages, choose_passages, digest_errors
from infofilm.research_harness import HarnessError, QwenClient, ResearchTools, run_agent
from infofilm.research_sources import readability_error


class ResearchHarnessTests(unittest.TestCase):
    def test_reasoning_only_response_retries_and_preserves_tool_call_without_spending_search_budget(self):
        import httpx
        payloads=[]
        call={'id':'search-1','type':'function','function':{'name':'web_search','arguments':'{"query":"Mughal empire"}'}}
        def respond(request):
            payloads.append(json.loads(request.content))
            message={'content':'','reasoning_content':'Internal reasoning that is not source evidence.'} if len(payloads)==1 else {'content':None,'tool_calls':[call]}
            return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':'length' if len(payloads)==1 else 'tool_calls'}]})
        client=QwenClient('http://example.org/v1','qwen')
        client.client.close()
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            client.client=transport
            result=client.complete([{'role':'user','content':'Research the Mughal empire.'}])
        self.assertEqual(result['tool_calls'],[call])
        self.assertEqual(len(payloads),2)
        self.assertEqual(payloads[0]['messages'],payloads[1]['messages'])
        self.assertIs(payloads[0]['chat_template_kwargs']['enable_thinking'],False)
        self.assertLessEqual(payloads[0]['max_tokens'],5000)
        self.assertTrue(client.last_response_reports[0]['reasoning_only'])
        self.assertTrue(client.last_response_reports[1]['usable'])

    def test_persistent_empty_responses_fail_after_bounded_retries_instead_of_becoming_evidence(self):
        import httpx
        calls=[]
        def respond(request):
            calls.append(request)
            return httpx.Response(200,json={'choices':[{'message':{'content':'','reasoning_content':'Thoughts only'},'finish_reason':'length'}]})
        client=QwenClient('http://example.org/v1','qwen')
        client.client.close()
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            client.client=transport
            with self.assertRaisesRegex(HarnessError,'after 3 attempts'):
                client.complete([{'role':'user','content':'Research'}])
        self.assertEqual(len(calls),3)

    def test_subprocess_failure_exposes_the_underlying_research_error(self):
        from infofilm.core import Store,write
        from infofilm.pipeline import research
        import sys
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory))
            def fail(*args,**kwargs):
                request=json.loads(Path(args[0][-1]).read_text())
                write(Path(request['directory'])/'failure.json',{'error':'Qwen returned no answer after 3 attempts'})
                raise subprocess.CalledProcessError(1,args[0])
            try:
                with patch('infofilm.pipeline.subprocess.run',side_effect=fail):
                    with self.assertRaisesRegex(RuntimeError,'Research failed: Qwen returned no answer'):
                        research({'research_python':sys.executable,'kbagent_root':'/unused','reviewer_url':'http://example.org/v1'},store,'History')
            finally:
                store.db.close()

    def test_first_pass_keeps_traceable_source_offsets_after_site_navigation(self):
        page = ("[Home](https://example.org/) " * 180 + "\n\nFrom Wikipedia, the free encyclopedia\n\n" +
                "A Roman state continued in the east after its western government disappeared. " * 25 +
                "\n\nHistorians discuss several interacting causes rather than a single decisive event. " * 25)
        passages = choose_passages(page, "The decline of Roman civilization", budget=4400)
        self.assertGreater(len(passages), 0)
        self.assertTrue(all(page[p['offset_start']:p['offset_end']] == p['text'] for p in passages))
        self.assertLessEqual(sum(len(p['text']) for p in passages), 4400)
        self.assertTrue(all(p['offset_start'] > 1500 for p in passages))

    def test_tool_returns_digest_and_keeps_full_source_for_archive(self):
        tools = ResearchTools(tavily=None, obscura_path=Path('/unused'))
        url = 'https://example.org/source'
        full = 'A long and useful source paragraph. ' * 200
        tools.fetch_cache[url] = full
        calls = []
        tools.source_summarizer = lambda source, text: calls.append((source, text)) or {
            'text':'A short sourced explanation.', 'claims':[{'claim':'A short sourced explanation.', 'passage_ids':['P01']}]}
        first = tools.fetch(url)
        second = tools.fetch(url)
        self.assertEqual(first['content'], 'A short sourced explanation.')
        self.assertNotIn(full[:100], json.dumps(first))
        self.assertEqual(tools.fetch_cache[url], full)
        self.assertTrue(second['cached'])
        self.assertEqual(len(calls), 1)

    def test_accumulated_context_is_bounded_without_breaking_tool_call_ids(self):
        messages = [{'role':'system','content':'Research carefully.'},
                    {'role':'assistant','tool_calls':[{'id':'fetch-1','type':'function','function':{'name':'fetch_url','arguments':'{}'}}]},
                    {'role':'tool','tool_call_id':'fetch-1','name':'fetch_url','content':json.dumps({'url':'https://example.org','content':'a ' * 9000})},
                    {'role':'user','content':'Synthesize the cited answer.'}]
        value, report = bound_messages(messages, limit=5000)
        self.assertTrue(report['compacted'])
        self.assertLessEqual(report['sent_message_bytes'], 5000)
        self.assertEqual(value[2]['tool_call_id'], 'fetch-1')
        self.assertEqual(json.loads(value[2]['content'])['url'], 'https://example.org')
        self.assertEqual(messages[2]['content'], json.dumps({'url':'https://example.org','content':'a ' * 9000}))

    def test_summary_claims_must_reference_actual_passage_ids(self):
        self.assertEqual(digest_errors({'claims':[{'claim':'This source supports a qualified causal claim.', 'passage_ids':['P01']},
                                              {'claim':'A second claim covers a later phase.', 'passage_ids':['P02']}]}, {'P01','P02'}), [])
        self.assertTrue(digest_errors({'claims':[{'claim':'This source supports a qualified causal claim.', 'passage_ids':['P99']},
                                             {'claim':'A second claim covers a later phase.', 'passage_ids':['P02']}]}, {'P01','P02'}))

    def test_pdf_is_extracted_before_the_model_sees_it(self):
        tools = ResearchTools(tavily=None, obscura_path=Path('/unused'))
        url = 'https://example.org/nutrition.PDF?download=1'
        text = 'A readable source paragraph with useful evidence. ' * 10
        summarizer = Mock(return_value={'text': 'A supported summary.', 'usable': True})
        tools.source_summarizer = summarizer
        with patch('infofilm.research_harness.fetch_pdf_text', return_value=text) as extract, \
             patch('infofilm.research_harness.subprocess.run') as obscura:
            first = tools.fetch(url)
            tools.fetch(url)
        extract.assert_called_once_with(url, tools.fetch_timeout, None)
        obscura.assert_not_called()
        summarizer.assert_called_once_with(url, text)
        self.assertEqual(first['content'], 'A supported summary.')
        self.assertIn(url, tools.fetched_urls)

    def test_old_raw_pdf_cache_is_replaced_with_readable_text(self):
        tools = ResearchTools(tavily=None, obscura_path=Path('/unused'))
        url = 'https://example.org/download'
        text = 'A readable PDF source paragraph with useful evidence. ' * 10
        tools.fetch_cache[url] = '%PDF-1.5\n' + '\ufffd' * 300
        tools.digest_cache[url] = {'text': 'A stale invalid digest.'}
        tools.source_summarizer = Mock(return_value={'text': 'New supported summary.'})
        with patch('infofilm.research_harness.fetch_pdf_text', return_value=text):
            result = tools.fetch(url)
        tools.source_summarizer.assert_called_once_with(url, text)
        self.assertEqual(tools.fetch_cache[url], text)
        self.assertEqual(result['content'], 'New supported summary.')

    def test_unreadable_pdf_is_cached_as_skipped_and_never_sent_to_model(self):
        tools = ResearchTools(tavily=None, obscura_path=Path('/unused'))
        url = 'https://example.org/scanned.pdf'
        tools.source_summarizer = Mock()
        with patch('infofilm.research_harness.fetch_pdf_text', side_effect=ValueError('PDF requires OCR')) as extract:
            first = tools.fetch(url)
            second = tools.fetch(url)
        extract.assert_called_once()
        tools.source_summarizer.assert_not_called()
        self.assertFalse(first['usable'])
        self.assertTrue(second['cached'])
        self.assertEqual(tools.fetches_used, 1)
        self.assertNotIn(url, tools.fetched_urls)

    def test_binary_source_is_rejected_before_summarization(self):
        tools = ResearchTools(tavily=None, obscura_path=Path('/unused'))
        url = 'https://example.org/binary'
        tools.fetch_cache[url] = '\ufffd\x00opaque' * 100
        tools.source_summarizer = Mock()
        result = tools.fetch(url)
        tools.source_summarizer.assert_not_called()
        self.assertFalse(result['usable'])
        self.assertNotIn(url, tools.fetched_urls)
        self.assertIsNone(readability_error('A readable Hindi passage: यह एक पढ़ने योग्य स्रोत है। ' * 10))

    def test_unusable_source_and_single_supported_claim_are_valid(self):
        self.assertEqual(digest_errors({'status': 'unusable', 'claims': [],
                                       'reason': 'There are no relevant facts in these passages.'}, {'P01'}), [])
        # Older cached model refusals used cautions instead of a status field.
        self.assertEqual(digest_errors({'claims': [], 'cautions': ['Only unreadable binary data was supplied.']}, {'P01'}), [])
        self.assertEqual(digest_errors({'claims': [{'claim': 'One relevant qualified fact is enough for this narrow source.',
                                                   'passage_ids': ['P01']}]}, {'P01'}), [])
        self.assertTrue(digest_errors({'status': 'unusable', 'claims': [], 'reason': ''}, {'P01'}))
        self.assertTrue(digest_errors({'claims': [{'claim': 'A supported claim with malformed passage IDs.',
                                                  'passage_ids': [{'id': 'P01'}]}]}, {'P01'}))

    def test_failed_source_summary_does_not_stop_research_or_allow_its_citation(self):
        bad, good = 'https://example.org/unusable', 'https://example.org/readable'
        tools = ResearchTools(tavily=None, obscura_path=Path('/unused'))
        tools.fetch_cache = {url: 'Readable source material. ' * 20 for url in (bad, good)}
        calls = []
        def summarize(url, text):
            calls.append(url)
            if url == bad:
                raise HarnessError('Source digest needs structural repair')
            return {'text': 'A useful qualified explanation.'}
        tools.source_summarizer = summarize
        def fetch_call(url, index):
            return {'id': str(index), 'type': 'function', 'function': {
                'name': 'fetch_url', 'arguments': json.dumps({'url': url})}}
        qwen = Mock()
        qwen.complete.side_effect = [
            {'tool_calls': [fetch_call(bad, 1), fetch_call(good, 2)]},
            {'content': f'A supported explanation. [Source]({good})'}]
        answer = run_agent('Explain the subject.', qwen, tools)
        self.assertIn(good, answer)
        self.assertEqual(tools.fetched_urls, {good})
        tools.fetch(bad)
        self.assertEqual(calls, [bad, good])

    def test_source_without_claims_is_excluded_from_evidence(self):
        tools = ResearchTools(tavily=None, obscura_path=Path('/unused'))
        url = 'https://example.org/irrelevant'
        tools.fetch_cache[url] = 'A readable but irrelevant source. ' * 20
        tools.source_summarizer = Mock(return_value={
            'usable': False, 'claims': [], 'text': '', 'reason': 'This page has no relevant supported claims.'})
        result = tools.fetch(url)
        self.assertFalse(result['usable'])
        self.assertNotIn(url, tools.fetched_urls)


if __name__ == '__main__':
    unittest.main()
