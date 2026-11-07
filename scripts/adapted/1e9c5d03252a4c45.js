// Adapted by SelfSurge: normalize Quantumult X $done results for Surge.
(function ($done) {
var obj=JSON.parse($response.body);obj.dt.data=Object.values(obj.dt.data).filter(a=>-1===a.jumpTypeString.indexOf("广告")),$done({body:JSON.stringify(obj)});

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
