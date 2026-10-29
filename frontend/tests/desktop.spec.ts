import {test,expect,Page} from '@playwright/test';
import {readFileSync,mkdirSync} from 'node:fs';
import {resolve} from 'node:path';
const credentials=JSON.parse(readFileSync(resolve('../data/demo-credentials.json'),'utf8'));
async function login(page:Page,username:string){await page.goto('/');await page.getByLabel('用户名').fill(username);await page.getByLabel('密码',{exact:true}).fill(credentials[username].password);await page.getByRole('button',{name:'登录 DeskFlow'}).click();await expect(page.getByRole('heading',{name:'工单中心.'})).toBeVisible();}
test('employee request, department approval, verified execution and confirmation',async({browser})=>{
 const ec=await browser.newContext(),ac=await browser.newContext();const employee=await ec.newPage(),approver=await ac.newPage();
 const errors:string[]=[];employee.on('pageerror',e=>errors.push(e.message));
 await login(employee,'employee');await employee.getByRole('button',{name:'新建工单',exact:true}).click();
 await employee.locator('.modal').getByRole('button',{name:'临时权限',exact:true}).click();
 const title='浏览器验收 · 出差 VPN '+Date.now();await employee.getByLabel('工单标题').fill(title);await employee.getByLabel('问题描述').fill('出差需要访问办公网络，请开通临时权限。');await employee.getByLabel('申请用途').fill('客户现场远程办公');await employee.getByRole('button',{name:'提交工单'}).click();
 await expect(employee.getByRole('heading',{name:title})).toBeVisible();await employee.getByRole('button',{name:'开始 / 重新分析'}).click();
 await expect(employee.locator('.run-status')).toContainText('待审批',{timeout:25000});
 await login(approver,'approver');await approver.locator('nav').getByRole('button',{name:'审批中心'}).click();const card=approver.locator('article').filter({has:approver.getByRole('heading',{name:title})});await card.getByRole('button',{name:'批准执行'}).click();
 await expect(employee.locator('.page-heading .badge')).toHaveText('待确认',{timeout:25000});await expect(employee.getByText('已验证',{exact:true})).toBeVisible();
 mkdirSync(resolve('../artifacts'),{recursive:true});await employee.screenshot({path:resolve('../artifacts/ticket-detail.png'),fullPage:true});
 await employee.getByRole('button',{name:'确认已解决'}).click();await expect(employee.locator('.page-heading .badge')).toHaveText('已关闭');expect(errors).toEqual([]);
 await ec.close();await ac.close();
});
test('administrator pages and connection failure result',async({page})=>{
 const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));await login(page,'admin');
 await page.screenshot({path:resolve('../artifacts/workspace.png'),fullPage:true});
 for(const name of ['知识库','演示业务系统','运行与评测','设置']){await page.locator('nav').getByRole('button',{name,exact:true}).click();await expect(page.locator('.page-heading h1')).toContainText(name);}
 const card=page.locator('section.card').filter({has:page.getByRole('heading',{name:'Embedding 向量模型'})});await expect(card.getByLabel('模型 ID')).toHaveValue('text-embedding-v4');await card.getByLabel('Base URL').fill('http://127.0.0.1:9/v1');await card.getByLabel('API Key',{exact:true}).fill('sk-mock-no-real-key');await card.getByRole('button',{name:'测试连接'}).click();await expect(card.locator('.test-result')).toContainText(/失败：(无法连接向量接口|模型请求失败 HTTP 502)/,{timeout:20000});await expect(card.getByLabel('Base URL')).toHaveValue('http://127.0.0.1:9/v1');await expect(card.getByLabel('API Key',{exact:true})).toHaveValue('sk-mock-no-real-key');
 await page.screenshot({path:resolve('../artifacts/settings.png'),fullPage:true});expect(errors).toEqual([]);
});
