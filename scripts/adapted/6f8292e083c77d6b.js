// Adapted by SelfSurge: normalize Quantumult X $done results for Surge.
(function ($done) {
/*

Author：@ddgksf2013

通知频道：https://t.me/ddgksf2021

*/
var body = $response.body;
var ddgksf2013 = JSON.parse(body);

if (ddgksf2013.Variables.data.threaddetail) 
{
ddgksf2013.Variables.data.threaddetail.tagadv = "";
	ddgksf2013.Variables.data.threaddetail.threadapp_ad_video = [];
	ddgksf2013.Variables.data.threaddetail.pingyouadv = "";
	ddgksf2013.Variables.data.threaddetail.middleadv = "";
	ddgksf2013.Variables.data.threaddetail.bottomadv = "";
	ddgksf2013.Variables.data.threaddetail.appdetailadv = [];
}
//delete ddgksf2013.Variables.data.threaddetail.threadapp_ad_video;
//delete ddgksf2013.Variables.data.threaddetail.tagadv;
//delete ddgksf2013.Variables.data.threaddetail.middleadv;
//delete ddgksf2013.Variables.data.threaddetail.pingyouadv;
//delete ddgksf2013.Variables.data.threaddetail.bottomadv;
//delete ddgksf2013.Variables.data.threaddetail.appdetailadv;
	
$done({body: JSON.stringify(ddgksf2013)});
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
