// Adapted by SelfSurge: normalize Quantumult X $done results for Surge.
(function ($done) {
/***********************************************
> 应用名称：墨鱼自用奇点阅读去广告脚本
> 脚本作者：@ddgksf2013
> 微信账号：墨鱼手记
> 更新时间：2024-09-14
> 通知频道：https://t.me/ddgksf2021
> 特别提醒：如需转载请注明出处，谢谢合作！
***********************************************/


const version = 'V1.0.0';


var obj=JSON.parse($response.body);const propertiesToDelete=["ads2","adSource2","third"];for(const prop of propertiesToDelete)obj.data?.[prop]&&(obj.data[prop]={});$done({body:JSON.stringify(obj)});

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
