import io
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import server
import numpy as np
from PIL import Image
import run_psd

class LayerWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http=ThreadingHTTPServer(('127.0.0.1',0),server.LayerRebuildHandler)
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True); cls.thread.start()
        cls.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); cls.thread.join()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.directory=Path(self.temp.name)
        self.job={'id':'test','filename':'test.png','status':'ready','message':'Test','created_at':server.now(),
                  'directory':self.directory,'source':self.directory/'source.png','vision_masks':[{'name':'group','mask':'dummy'}],
                  'vision_text':[],'dimensions':{'width':10,'height':10}}
        server.JOBS['test']=self.job
    def tearDown(self):
        server.JOBS.clear(); self.temp.cleanup()
    def request(self,path,body=None,content_type='application/json'):
        data=json.dumps(body).encode() if isinstance(body,dict) else body
        request=urllib.request.Request(f'http://127.0.0.1:{self.http.server_port}'+path,data=data,headers={'Content-Type':content_type})
        try:
            with self.opener.open(request) as r: return r.status,r.read()
        except urllib.error.HTTPError as e: return e.code,e.read()
    def test_external_segmentation_needs_separate_consent(self):
        code,data=self.request('/api/build',{'job_id':'test','required_elements':['glass'],'fill_method':'smooth'})
        self.assertEqual(code,400); self.assertIn(b'SAM3',data)
        self.assertFalse((self.directory/'recipe.json').exists())
    def test_external_fill_needs_consent(self):
        self.assertEqual(self.request('/api/build',{'job_id':'test','fill_method':'flux'})[0],400)
    def test_invalid_requirement_type_rejected(self):
        self.assertEqual(self.request('/api/build',{'job_id':'test','required_elements':'glass'})[0],400)
    def test_queued_job_cannot_start_again(self):
        self.job['status']='queued'
        self.assertEqual(self.request('/api/build',{'job_id':'test'})[0],409)
    def test_download_requires_review(self):
        bundle=self.directory/'draft.zip'; bundle.write_bytes(b'fixture')
        self.job.update(bundle=bundle,status='review')
        self.assertEqual(self.request('/api/download?job=test')[0],409)
        self.assertEqual(self.request('/api/confirm-review',{'job_id':'test','acknowledge_limitations':False})[0],400)
        self.assertEqual(self.request('/api/confirm-review',{'job_id':'test','acknowledge_limitations':True})[0],200)
        self.assertEqual(self.request('/api/download?job=test')[0],200)
    def test_corrupt_image_rejected_at_upload(self):
        body=b'--qa\r\nContent-Disposition: form-data; name="image"; filename="bad.png"\r\nContent-Type: image/png\r\n\r\nnot image data\r\n--qa--\r\n'
        with patch.object(server,'JOB_ROOT',self.directory):
            code,_=self.request('/api/upload',body,'multipart/form-data; boundary=qa')
        self.assertEqual(code,400)
        self.assertEqual(list(self.directory.iterdir()),[])
    def test_layer_path_traversal_rejected(self):
        self.assertEqual(self.request('/api/layer-preview?job=test&layer=../../source')[0],404)

    def test_requested_object_not_duplicated_in_local_group(self):
        source=self.directory/'source.png'; mask=self.directory/'mask.png'
        Image.new('RGBA',(100,100),'red').save(source)
        Image.new('L',(100,100),255).save(mask)
        alpha=np.zeros((100,100),dtype=np.uint8); alpha[:,:50]=255
        with patch.object(run_psd.s,'load_api_key',return_value='test-only'), patch.object(run_psd.P,'cut_element',return_value=[Image.fromarray(alpha)]), patch.object(run_psd.P,'write_psd',return_value=3) as writer:
            run_psd.run_recipe({'image':str(source),'output':str(self.directory/'test.psd'),
                'elements':[{'prompt':'left object'}],'local_masks':[{'name':'group','mask':str(mask)}]})
        stack=writer.call_args.args[0]
        self.assertEqual(len(stack),3)
        self.assertFalse(np.any((stack[1][1][:,:,3]>0)&(stack[2][1][:,:,3]>0)))

    def test_required_miss_stops_export(self):
        source=self.directory/'source.png'; Image.new('RGBA',(100,100),'red').save(source)
        with patch.object(run_psd.s,'load_api_key',return_value='test-only'), patch.object(run_psd.P,'cut_element',return_value=[]), patch.object(run_psd.P,'write_psd') as writer:
            with self.assertRaisesRegex(RuntimeError,'Required layers'):
                run_psd.run_recipe({'image':str(source),'output':str(self.directory/'test.psd'),'elements':[{'prompt':'missing object'}]})
            writer.assert_not_called()

if __name__ == '__main__': unittest.main()
