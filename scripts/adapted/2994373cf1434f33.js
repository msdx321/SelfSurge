// Adapted by SelfSurge: normalize Quantumult X $done results for Surge.
(function ($done) {
/***********************************************
> 应用名称：墨鱼自用航旅纵横脚本
> 脚本作者：@ddgksf2013
> 微信账号：墨鱼手记
> 更新时间：2022-10-26
> 通知频道：https://t.me/ddgksf2021
> 特别提醒：如需转载请注明出处，谢谢合作！
***********************************************/


const version = 'V1.0.2';

var ua=$request.headers.rpid||$request.headers.Rpid;ua.includes("10000012")||ua.includes("1000019")?$done({status:"HTTP/1.1 404 Not Found"}):$done({});

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
