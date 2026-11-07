// Adapted by SelfSurge: normalize Quantumult X $done results for Surge.
(function ($done) {
/**********************************************
> 应用名称：墨鱼自用朴朴超市去广告脚本
> 脚本作者：@ddgksf2013
> 微信账号：墨鱼手记
> 更新时间：2023-11-19
> 通知频道：https://t.me/ddgksf2021
> 特别提醒：如需转载请注明出处，谢谢合作！
> 脚本说明：去除开屏广告、搜索轮播填充
**********************************************/


const version = 'V1.0.0';

var obj=JSON.parse($response.body);obj.data&&(obj.data=Object.values(obj.data).filter(o=>!(320==o.position_type||710==o.position_type||50==o.position_type))),$done({body:JSON.stringify(obj)});

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
