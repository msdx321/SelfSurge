// Adapted by SelfSurge: normalize Quantumult X $done results for Surge.
(function ($done) {




let obj=JSON.parse($response.body);
/*京喜APP*/
if (/^https?:\/\/api\.m\.jd\.com\/api\?functionId=delivery_show/.test($request.url)) { 
    obj.data.materialList.startTime=3667476800000;
    obj.data.materialList.endTime  =3667908800000;
}
$done({body:JSON.stringify(obj)});

})((result) => {
  if (result && typeof result === 'object') {
    if (false && !result.response && Object.keys(result).length) {
      result = {response: result};
    }
    const response = result.response || result;
    if (typeof response.status === 'string') {
      const code = response.status.match(/^(?:HTTP\/\S+\s+)?(\d{3})(?:\s|$)/);
      if (code) response.status = Number(code[1]);
    }
  }
  $done(result);
});
